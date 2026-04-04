# types/build_wikidata_types.py

"""
Build Wikidata type vocabulary file (types/data/wikidata.json).

Queries an ES places index for distinct P31 (instance-of) Q-items used
in Wikidata place records, then fetches English labels from the Wikidata API.

Usage:
    python -m typesystem.build_wikidata_types --es-host URL
    python -m typesystem.build_wikidata_types --es-host URL --min-count 10
"""

import json
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode

OUTPUT_FILE = Path(__file__).parent / "data" / "wikidata.json"

# Wikidata API batch size for label fetching (max 50 per request)
WD_BATCH_SIZE = 50


def query_es_types(es_host, min_count=1):
    """
    Query ES for all distinct types.identifier values where types.label = 'wikidata'.
    Returns list of (qid, count) tuples sorted by count descending.
    """
    from typesystem.es_client import create_client

    es = create_client(es_host, request_timeout=180)
    print(f"Querying ES at {es_host} for Wikidata P31 type distribution ...")

    resp = es.search(
        index="places_*",
        size=0,
        aggs={
            "types_nested": {
                "nested": {"path": "types"},
                "aggs": {
                    "wd_only": {
                        "filter": {"term": {"types.label": "wikidata"}},
                        "aggs": {
                            "by_identifier": {
                                "terms": {
                                    "field": "types.identifier",
                                    "size": 50000,
                                    "min_doc_count": min_count,
                                }
                            }
                        },
                    }
                },
            }
        },
    )
    buckets = resp["aggregations"]["types_nested"]["wd_only"]["by_identifier"]["buckets"]

    results = [(b["key"], b["doc_count"]) for b in buckets]
    print(f"  -> Found {len(results)} distinct Q-items (min_count={min_count})")
    return results


def fetch_wikidata_labels(qids):
    """
    Fetch English labels for a list of Q-IDs from the Wikidata API.
    Returns dict: qid → label string.
    """
    labels = {}
    batches = [qids[i:i + WD_BATCH_SIZE] for i in range(0, len(qids), WD_BATCH_SIZE)]

    print(f"Fetching labels for {len(qids)} Q-items in {len(batches)} batches ...")

    for i, batch in enumerate(batches):
        if (i + 1) % 10 == 0:
            print(f"  ... batch {i + 1}/{len(batches)}")

        params = urlencode({
            "action": "wbgetentities",
            "ids": "|".join(batch),
            "props": "labels|descriptions",
            "languages": "en",
            "format": "json",
        })
        url = f"https://www.wikidata.org/w/api.php?{params}"
        req = Request(url, headers={"User-Agent": "WHG-indexing/1.0 (whgazetteer.org)"})

        try:
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            for qid, entity in data.get("entities", {}).items():
                label_obj = entity.get("labels", {}).get("en", {})
                desc_obj = entity.get("descriptions", {}).get("en", {})
                labels[qid] = {
                    "label": label_obj.get("value", ""),
                    "description": desc_obj.get("value", ""),
                }
        except Exception as e:
            print(f"  Warning: batch {i + 1} failed: {e}")

        # Rate limiting — Wikidata API is generous but be polite
        time.sleep(0.1)

    return labels


def build_output(type_counts, wd_labels):
    """Structure the output JSON."""
    values = []
    for qid, count in type_counts:
        info = wd_labels.get(qid, {})
        entry = {
            "value": qid,
            "label": info.get("label", ""),
            "description": info.get("description", ""),
            "count": count,
        }
        values.append(entry)

    # Sort by count descending
    values.sort(key=lambda e: -e["count"])

    return {
        "namespace": "wikidata",
        "source": "P31 (instance of) claims on Wikidata place entities",
        "total_distinct_types": len(values),
        "values": values,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Build Wikidata type vocabulary file"
    )
    parser.add_argument("--es-host", required=True, help="ES host URL")
    parser.add_argument("--min-count", type=int, default=1,
                        help="Minimum doc count to include a Q-item (default: 1)")
    args = parser.parse_args()

    type_counts = query_es_types(args.es_host, args.min_count)

    qids = [qid for qid, _ in type_counts]
    wd_labels = fetch_wikidata_labels(qids)

    output = build_output(type_counts, wd_labels)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Written {len(type_counts)} types to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


