# types/build_geonames_types.py

"""
Build GeoNames type vocabulary file (types/data/geonames.json).

Downloads the official GeoNames featureCodes_en.txt and organises entries
by feature class.  Optionally queries an ES places index to add doc counts.

Usage:
    python -m typesystem.build_geonames_types                     # local only
    python -m typesystem.build_geonames_types --es-host URL       # with ES counts
"""

import json
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen, Request

FEATURE_CODES_URL = "https://download.geonames.org/export/dump/featureCodes_en.txt"
OUTPUT_FILE = Path(__file__).parent / "data" / "geonames.json"

# Human-readable feature class names
FEATURE_CLASS_LABELS = {
    "A": "Administrative boundary features",
    "H": "Hydrographic features",
    "L": "Area features (parks, areas)",
    "P": "Populated places",
    "R": "Road / railroad features",
    "S": "Spot / building / farm features",
    "T": "Hypsographic / terrain features",
    "U": "Undersea features",
    "V": "Vegetation features",
}


def fetch_feature_codes():
    """Download and parse featureCodes_en.txt → dict keyed by feature class."""
    print(f"Downloading {FEATURE_CODES_URL} ...")
    req = Request(FEATURE_CODES_URL, headers={"User-Agent": "WHG-indexing/1.0"})
    with urlopen(req) as resp:
        raw = resp.read().decode("utf-8")

    classes = defaultdict(list)

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue

        code_part = parts[0]  # e.g. "P.PPL" or "null"
        name = parts[1] if len(parts) > 1 else ""
        description = parts[2] if len(parts) > 2 else ""

        if "." not in code_part:
            continue

        fclass, fcode = code_part.split(".", 1)
        if not fclass or not fcode:
            continue

        entry = {
            "value": fcode,
            "feature_class": fclass,
            "name": name,
            "description": description,
        }
        classes[fclass].append(entry)

    return dict(classes)


def add_es_counts(classes, es_host):
    """Query ES to get doc counts per (feature_class, feature_code) pair."""
    from elasticsearch import Elasticsearch

    es = Elasticsearch(es_host, request_timeout=120)
    print(f"Querying ES at {es_host} for GeoNames type counts ...")

    # Nested aggregation on types.identifier where types.label matches a
    # GeoNames feature class letter
    body = {
        "size": 0,
        "query": {"term": {"namespace": "gn"}},
        "aggs": {
            "types_nested": {
                "nested": {"path": "types"},
                "aggs": {
                    "by_source_label": {
                        "terms": {
                            "field": "types.sourceLabel",
                            "size": 10000,
                        }
                    }
                },
            }
        },
    }

    try:
        resp = es.search(index="places_*", body=body)
        buckets = resp["aggregations"]["types_nested"]["by_source_label"]["buckets"]
        counts = {}
        for b in buckets:
            # sourceLabel format: "P.PPL"
            counts[b["key"]] = b["doc_count"]

        # Merge counts into classes
        for fclass, entries in classes.items():
            for entry in entries:
                key = f"{fclass}.{entry['value']}"
                entry["count"] = counts.get(key, 0)

        print(f"  -> Merged counts for {len(counts)} sourceLabel values")
    except Exception as e:
        print(f"  Warning: ES query failed ({e}), counts will be 0")


def build_output(classes):
    """Structure the output JSON."""
    output = {}
    for fclass in sorted(classes.keys()):
        entries = classes[fclass]
        # Sort by count descending (if available), then by code
        entries.sort(key=lambda e: (-e.get("count", 0), e["value"]))
        output[fclass] = {
            "feature_class": fclass,
            "label": FEATURE_CLASS_LABELS.get(fclass, fclass),
            "total_codes": len(entries),
            "values": entries,
        }
    return output


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Build GeoNames type vocabulary file"
    )
    parser.add_argument("--es-host", help="ES host URL for doc counts")
    args = parser.parse_args()

    classes = fetch_feature_codes()
    total = sum(len(v) for v in classes.values())
    print(f"Parsed {total} feature codes across {len(classes)} classes")

    if args.es_host:
        add_es_counts(classes, args.es_host)

    output = build_output(classes)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


