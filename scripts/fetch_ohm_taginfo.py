#!/usr/bin/env python3
"""
Fetch OpenHistoricalMap tag value statistics via the OHM Overpass API.

OHM's TagInfo API blocks programmatic access, so this script uses OHM's
own Overpass instance to sample named features for each tag key and
collect value distributions with temporal coverage statistics.

Output: JSON file with per-key value lists, counts, and temporal coverage.
Also prints a Markdown-formatted summary for inclusion in documentation.

OHM Overpass: https://overpass-api.openhistoricalmap.org/api/interpreter
"""

import json
import time
import urllib.request
import urllib.parse
from collections import Counter
from pathlib import Path

OVERPASS_URL = "https://overpass-api.openhistoricalmap.org/api/interpreter"

# All tag keys to query, grouped by tier
# Tier assignment follows the same criteria as the OSM inventory:
# gazetteer relevance, historical depth, AAT mappability, volume
KEYS_BY_TIER = {
    "tier1_place": ["place", "historic", "boundary", "natural", "waterway"],
    "tier2_high": ["amenity", "tourism", "leisure", "man_made", "military",
                   "landuse", "railway"],
    "tier3_medium": ["building", "shop", "office", "bridge", "tunnel",
                     "aeroway", "power", "healthcare"],
    "tier4_low": ["geological"],
}

# Maximum sample size per key (Overpass limit for tags-only output)
SAMPLE_LIMIT = 5000


def query_overpass(query_str: str, timeout: int = 120) -> dict:
    """Execute an Overpass query against the OHM instance."""
    data = urllib.parse.urlencode({"data": query_str}).encode()
    req = urllib.request.Request(
        OVERPASS_URL,
        data=data,
        headers={"User-Agent": "WHG-indexing/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_key_count(key: str) -> dict:
    """Get total count of named features for a key (nodes + ways + relations)."""
    q = f'[out:json][timeout:30]; nwr["{key}"]["name"]; out count;'
    result = query_overpass(q)
    elements = result.get("elements", [])
    if elements:
        tags = elements[0].get("tags", {})
        return {
            "nodes": int(tags.get("nodes", 0)),
            "ways": int(tags.get("ways", 0)),
            "relations": int(tags.get("relations", 0)),
            "total": int(tags.get("total", 0)),
        }
    return {"nodes": 0, "ways": 0, "relations": 0, "total": 0}


def fetch_key_values(key: str, limit: int = SAMPLE_LIMIT) -> dict:
    """
    Sample named features with this key, returning value distributions
    and temporal coverage statistics.
    """
    q = f'''[out:json][timeout:90];
(
  nwr["{key}"]["name"];
);
out tags {limit};'''

    result = query_overpass(q)
    elements = result.get("elements", [])

    value_counts = Counter()
    has_start_date = 0
    has_end_date = 0
    has_any_temporal = 0

    for el in elements:
        tags = el.get("tags", {})
        val = tags.get(key, "")
        value_counts[val] += 1
        has_sd = "start_date" in tags
        has_ed = "end_date" in tags
        if has_sd:
            has_start_date += 1
        if has_ed:
            has_end_date += 1
        if has_sd or has_ed:
            has_any_temporal += 1

    total_sampled = len(elements)

    values = []
    for val, count in value_counts.most_common():
        values.append({
            "value": val,
            "count": count,
            "fraction": count / total_sampled if total_sampled > 0 else 0,
        })

    return {
        "sampled": total_sampled,
        "with_start_date": has_start_date,
        "with_end_date": has_end_date,
        "with_any_temporal": has_any_temporal,
        "temporal_pct": round(100 * has_any_temporal / max(1, total_sampled), 1),
        "start_date_pct": round(100 * has_start_date / max(1, total_sampled), 1),
        "end_date_pct": round(100 * has_end_date / max(1, total_sampled), 1),
        "distinct_values": len(value_counts),
        "values": values,
    }


def format_count(n):
    """Human-readable count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def main():
    all_data = {}
    output_dir = Path(__file__).parent.parent / "developer"

    print("=" * 80)
    print("OpenHistoricalMap Tag Data Fetch (via Overpass)")
    print("=" * 80)

    # First pass: get total counts
    print("\n--- Total named feature counts ---\n")
    for tier_name, keys in KEYS_BY_TIER.items():
        for key in keys:
            print(f"  Counting: {key} ...", end=" ", flush=True)
            try:
                counts = fetch_key_count(key)
                print(f"{format_count(counts['total'])} named features")
                all_data[key] = {
                    "tier": tier_name,
                    "total_named_features": counts["total"],
                    "by_element_type": counts,
                }
            except Exception as e:
                print(f"FAILED: {e}")
                all_data[key] = {"tier": tier_name, "error": str(e)}
            time.sleep(1)

    # Second pass: sample values and temporal coverage
    print("\n--- Sampling values (up to 5000 per key) ---\n")
    for tier_name, keys in KEYS_BY_TIER.items():
        for key in keys:
            if "error" in all_data.get(key, {}):
                continue

            print(f"  Sampling: {key} ...", end=" ", flush=True)
            try:
                values_data = fetch_key_values(key)
                all_data[key].update(values_data)
                print(
                    f"OK — {values_data['distinct_values']} distinct values, "
                    f"temporal: {values_data['temporal_pct']}%"
                )
            except Exception as e:
                print(f"FAILED: {e}")
                all_data[key]["sample_error"] = str(e)
            time.sleep(2)  # Be polite — OHM is a community resource

    # Also get the global total
    print("\n--- Global named feature count ---")
    try:
        q = '[out:json][timeout:60]; nwr["name"]; out count;'
        result = query_overpass(q)
        elements = result.get("elements", [])
        if elements:
            tags = elements[0].get("tags", {})
            all_data["_global"] = {
                "total_named": int(tags.get("total", 0)),
                "nodes": int(tags.get("nodes", 0)),
                "ways": int(tags.get("ways", 0)),
                "relations": int(tags.get("relations", 0)),
            }
            print(f"  Total named features in OHM: {format_count(all_data['_global']['total_named'])}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Save raw JSON
    json_path = output_dir / "ohm-taginfo-data.json"
    with open(json_path, "w") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)
    print(f"\nRaw data saved to: {json_path}")

    # Print Markdown summary
    print("\n" + "=" * 80)
    print("MARKDOWN SUMMARY")
    print("=" * 80)

    for tier_name, keys in KEYS_BY_TIER.items():
        print(f"\n### {tier_name}\n")
        for key in keys:
            if key not in all_data or "error" in all_data[key]:
                print(f"**`{key}`**: fetch failed\n")
                continue

            kd = all_data[key]
            total = kd.get("total_named_features", 0)
            temporal = kd.get("temporal_pct", "?")
            distinct = kd.get("distinct_values", "?")
            print(
                f"**`{key}`** — {format_count(total)} named features, "
                f"{temporal}% with temporal tags, "
                f"{distinct} distinct values\n"
            )
            print("| Value | Count | % | start_date% | Description |")
            print("|-------|------:|--:|--:|-------------|")

            for v in kd.get("values", [])[:30]:
                pct = f"{v['fraction'] * 100:.1f}" if v["fraction"] else ""
                print(f"| `{v['value']}` | {v['count']} | {pct} | | |")

            remaining = kd.get("distinct_values", 0) - min(30, len(kd.get("values", [])))
            if remaining > 0:
                print(f"| ... | | | | *+{remaining} more values* |")
            print()


if __name__ == "__main__":
    main()

