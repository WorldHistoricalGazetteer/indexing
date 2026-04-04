# types/build_pleiades_types.py

"""
Build Pleiades type vocabulary file (types/data/pleiades.json).

Fetches the Pleiades place-types vocabulary JSON which already includes
`same_as` URIs (some linking to AAT concepts).

Usage:
    python -m typesystem.build_pleiades_types
    python -m typesystem.build_pleiades_types --es-host URL   # add doc counts
"""

import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request

PLEIADES_VOCAB_URL = "https://pleiades.stoa.org/vocabularies/place-types"
OUTPUT_FILE = Path(__file__).parent / "data" / "pleiades.json"

MAX_RETRIES = 4
RETRY_BACKOFF = [10, 30, 60, 120]  # seconds

# Pleiades (Plone) is wary of bot traffic — provide descriptive headers
# so the server knows who we are and that we want JSON (content negotiation).
REQUEST_HEADERS = {
    "User-Agent": "WHG-indexing/1.0 (World Historical Gazetteer; +https://whgazetteer.org)",
    "Accept": "application/json",
    "From": "whg@pitt.edu",
}


def fetch_pleiades_vocabulary():
    """Download Pleiades place-types vocabulary (with retries)."""
    req = Request(PLEIADES_VOCAB_URL, headers=REQUEST_HEADERS)

    for attempt in range(MAX_RETRIES + 1):
        try:
            print(f"Downloading {PLEIADES_VOCAB_URL} ...")
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data
        except (HTTPError, URLError, TimeoutError) as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt]
                print(f"  Attempt {attempt + 1} failed: {e}")
                print(f"  Retrying in {wait}s ...")
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"Failed to fetch Pleiades vocabulary after {MAX_RETRIES + 1} attempts: {e}"
                ) from e


def parse_vocabulary(raw_data):
    """
    Parse the Pleiades vocabulary JSON into structured entries.

    The Pleiades vocab JSON (via Accept: application/json content
    negotiation) is a list of dicts, each with:
      - id, title, description, same_as (string URI or None), hidden (bool)
    """
    entries = []

    # The response is a list when fetched via content negotiation
    items = raw_data if isinstance(raw_data, list) else raw_data.get("items", raw_data)

    if isinstance(items, dict):
        # Fallback: some vocab endpoints return {id: {...}, id: {...}}
        items = list(items.values())

    for item in items:
        if not isinstance(item, dict):
            continue

        identifier = item.get("id", item.get("identifier", ""))
        title = item.get("title", item.get("label", identifier))
        description = item.get("description", "")
        # same_as is a single URI string (or None) in the Pleiades response
        same_as_raw = item.get("same_as", item.get("sameAs"))
        # Pleiades uses "hidden" for deprecated terms
        deprecated = item.get("hidden", item.get("deprecated", False))

        if not identifier:
            continue

        # Normalise same_as to a list
        if same_as_raw is None:
            same_as = []
        elif isinstance(same_as_raw, str):
            same_as = [same_as_raw] if same_as_raw else []
        else:
            same_as = list(same_as_raw)

        # Extract AAT ID from same_as URIs
        aat_id = None
        aat_uri = None
        for uri in same_as:
            if "vocab.getty.edu/aat/" in uri:
                aat_uri = uri
                try:
                    aat_id = int(uri.split("/aat/")[-1].rstrip("/"))
                except ValueError:
                    pass
                break

        entry = {
            "value": identifier,
            "label": title,
            "description": description,
            "same_as": same_as,
            "deprecated": deprecated,
        }

        if aat_id is not None:
            entry["aat_id"] = aat_id
            entry["aat_uri"] = aat_uri

        entries.append(entry)

    return entries


def add_es_counts(entries, es_host):
    """Query ES to get doc counts per Pleiades type identifier."""
    from elasticsearch import Elasticsearch

    es = Elasticsearch(es_host, request_timeout=120)
    print(f"Querying ES at {es_host} for Pleiades type counts ...")

    try:
        resp = es.search(
            index="places_*",
            size=0,
            aggs={
                "types_nested": {
                    "nested": {"path": "types"},
                    "aggs": {
                        "pl_only": {
                            "filter": {"term": {"types.label": "pleiades"}},
                            "aggs": {
                                "by_identifier": {
                                    "terms": {
                                        "field": "types.identifier",
                                        "size": 5000,
                                    }
                                }
                            },
                        }
                    },
                }
            },
        )
        buckets = resp["aggregations"]["types_nested"]["pl_only"]["by_identifier"]["buckets"]
        counts = {b["key"]: b["doc_count"] for b in buckets}

        for entry in entries:
            entry["count"] = counts.get(entry["value"], 0)

        print(f"  -> Merged counts for {len(counts)} Pleiades types")
    except Exception as e:
        print(f"  Warning: ES query failed ({e}), counts will be absent")


def build_output(entries):
    """Structure the output JSON."""
    # Separate active from deprecated
    active = [e for e in entries if not e.get("deprecated")]
    deprecated = [e for e in entries if e.get("deprecated")]

    # Count those with AAT mappings
    with_aat = sum(1 for e in active if "aat_id" in e)

    # Sort active by count (if available) then by identifier
    active.sort(key=lambda e: (-e.get("count", 0), e["value"]))
    deprecated.sort(key=lambda e: e["value"])

    return {
        "namespace": "pleiades",
        "source": "Pleiades place-types vocabulary (https://pleiades.stoa.org/vocabularies/place-types)",
        "total_active_types": len(active),
        "total_deprecated_types": len(deprecated),
        "types_with_aat_mapping": with_aat,
        "values": active,
        "deprecated": deprecated,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Build Pleiades type vocabulary file"
    )
    parser.add_argument("--es-host", help="ES host URL for doc counts")
    args = parser.parse_args()

    raw_data = fetch_pleiades_vocabulary()
    entries = parse_vocabulary(raw_data)
    print(f"Parsed {len(entries)} Pleiades place types")

    with_aat = sum(1 for e in entries if "aat_id" in e)
    print(f"  -> {with_aat} already have AAT mappings from same_as URIs")

    if args.es_host:
        add_es_counts(entries, args.es_host)

    output = build_output(entries)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


