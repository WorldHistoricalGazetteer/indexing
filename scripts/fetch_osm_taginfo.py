#!/usr/bin/env python3
"""
Fetch OSM tag value statistics from the TagInfo API for all tag keys
relevant to WHG place type mapping.

Output: JSON file with per-key value lists, counts, and wiki descriptions.
Also prints a Markdown-formatted summary for inclusion in documentation.

TagInfo API docs: https://taginfo.openstreetmap.org/taginfo/apidoc
"""

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

TAGINFO_BASE = "https://taginfo.openstreetmap.org/api/4"

# All tag keys to query, grouped by tier
KEYS_BY_TIER = {
    "tier1_current": ["place", "natural", "water", "waterway", "historic", "landuse"],
    "tier2_high": ["amenity", "tourism", "leisure", "man_made", "boundary", "military"],
    "tier3_medium": ["aeroway", "railway", "geological", "power"],
    "tier_building": ["building"],
}

# For amenity, we also want to know how many have names
# TagInfo doesn't filter by name, but we can get key combinations


def fetch_key_values(key, rp=200):
    """Fetch all values for a tag key, sorted by count descending."""
    params = urllib.parse.urlencode({
        "key": key,
        "page": 1,
        "rp": rp,
        "sortname": "count",
        "sortorder": "desc",
        "format": "json",
    })
    url = f"{TAGINFO_BASE}/key/values?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "WHG-indexing/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_key_stats(key):
    """Fetch overall statistics for a tag key."""
    params = urllib.parse.urlencode({
        "key": key,
        "format": "json",
    })
    url = f"{TAGINFO_BASE}/key/stats?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "WHG-indexing/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_key_combinations(key, rp=20):
    """Fetch most common tag key combinations (e.g. what other keys
    appear on features with this key)."""
    params = urllib.parse.urlencode({
        "key": key,
        "page": 1,
        "rp": rp,
        "sortname": "together_count",
        "sortorder": "desc",
        "format": "json",
    })
    url = f"{TAGINFO_BASE}/key/combinations?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "WHG-indexing/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def format_count(n):
    """Human-readable count."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def main():
    all_data = {}
    output_dir = Path(__file__).parent.parent / "developer"

    print("=" * 80)
    print("OSM TagInfo Data Fetch")
    print("=" * 80)

    for tier_name, keys in KEYS_BY_TIER.items():
        for key in keys:
            print(f"\nFetching: {key} ...", end=" ", flush=True)
            try:
                values_data = fetch_key_values(key, rp=200)
                stats_data = fetch_key_stats(key)
                combos_data = fetch_key_combinations(key, rp=10)

                # Check how many features also have 'name' tag
                name_count = 0
                for combo in combos_data.get("data", []):
                    if combo.get("other_key") == "name":
                        name_count = combo.get("together_count", 0)
                        break

                total_count = sum(
                    s.get("count", 0)
                    for s in stats_data.get("data", [])
                )

                all_data[key] = {
                    "tier": tier_name,
                    "total_features": total_count,
                    "features_with_name": name_count,
                    "data_until": values_data.get("data_until"),
                    "total_distinct_values": values_data.get("total", 0),
                    "values": [
                        {
                            "value": v["value"],
                            "count": v["count"],
                            "fraction": v.get("fraction", 0),
                            "in_wiki": v.get("in_wiki", False),
                            "description": v.get("description"),
                        }
                        for v in values_data.get("data", [])
                    ],
                }

                print(
                    f"OK — {values_data.get('total', '?')} distinct values, "
                    f"total={format_count(total_count)}, "
                    f"with name={format_count(name_count)}"
                )

            except Exception as e:
                print(f"FAILED: {e}")
                all_data[key] = {"error": str(e)}

            time.sleep(0.5)  # Be polite to the API

    # Save raw JSON
    json_path = output_dir / "osm-taginfo-data.json"
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
            print(f"**`{key}`** — {format_count(kd['total_features'])} total features, "
                  f"{format_count(kd['features_with_name'])} with `name` tag, "
                  f"{kd['total_distinct_values']} distinct values\n")
            print(f"| Value | Count | % | Wiki | Description |")
            print(f"|-------|------:|--:|:----:|-------------|")

            for v in kd["values"][:50]:  # Top 50
                desc = (v["description"] or "")[:60]
                wiki = "✓" if v["in_wiki"] else ""
                pct = f"{v['fraction']*100:.1f}" if v["fraction"] else ""
                print(f"| `{v['value']}` | {format_count(v['count'])} | {pct} | {wiki} | {desc} |")

            remaining = kd["total_distinct_values"] - min(50, len(kd["values"]))
            if remaining > 0:
                print(f"| ... | | | | *+{remaining} more values* |")
            print()


if __name__ == "__main__":
    main()

