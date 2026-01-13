#!/usr/bin/env python3
"""
Test HDBSCAN clustering on real toponyms from Elasticsearch.
Run on cluster with: srun --time=00:10:00 --mem=8G python3 testing/test_hdbscan_clustering.py
"""

import sys
import os
import numpy as np
import hdbscan
from sklearn.metrics.pairwise import cosine_distances
from collections import defaultdict

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elasticsearch import Elasticsearch

# Configuration
ES_HOST = os.environ.get("ES_HOST", "http://htc-n30:9201")

def main():
    print("=" * 60)
    print("HDBSCAN CLUSTERING TEST ON REAL TOPONYMS")
    print("=" * 60)

    # Connect to ES
    print(f"\nConnecting to ES at {ES_HOST}...")
    es = Elasticsearch([ES_HOST])

    if not es.ping():
        print("ERROR: Cannot connect to Elasticsearch")
        sys.exit(1)

    # Check toponyms index
    count = es.count(index="toponyms")["count"]
    print(f"toponyms index has {count:,} documents")

    # Count documents with PanPhon embeddings
    emb_count = es.count(index="toponyms", query={"exists": {"field": "panphon_embedding"}})["count"]
    print(f"Documents with PanPhon embedding: {emb_count:,}")

    # Find places with many toponyms that have embeddings
    print("\nFinding places with many toponyms (with PanPhon)...")
    agg_result = es.search(
        index="toponyms",
        size=0,
        query={"exists": {"field": "panphon_embedding"}},
        aggs={
            "places": {
                "terms": {
                    "field": "attestations",
                    "size": 20,
                    "order": {"_count": "desc"}
                }
            }
        }
    )

    buckets = agg_result["aggregations"]["places"]["buckets"]
    print(f"\nTop 10 places by toponym count:")
    for b in buckets[:10]:
        print(f"  {b['key']}: {b['doc_count']} toponyms")

    if not buckets:
        print("No places found with PanPhon embeddings")
        sys.exit(1)

    # Test clustering on the top place
    test_place = buckets[0]['key']
    print(f"\n{'=' * 60}")
    print(f"TESTING CLUSTERING FOR: {test_place}")
    print("=" * 60)

    # Get all toponyms for this place
    result = es.search(
        index="toponyms",
        size=200,
        query={
            "bool": {
                "must": [
                    {"term": {"attestations": test_place}},
                    {"exists": {"field": "panphon_embedding"}}
                ]
            }
        },
        _source=["toponym_id", "name", "lang", "script", "panphon_embedding"]
    )

    hits = result["hits"]["hits"]
    print(f"Found {len(hits)} toponyms with embeddings")

    if len(hits) < 3:
        print("Need at least 3 toponyms for HDBSCAN test")
        sys.exit(1)

    # Extract data
    toponyms = []
    for h in hits:
        src = h["_source"]
        emb = src.get("panphon_embedding", [])
        if emb and len(emb) > 0:
            toponyms.append({
                "id": src.get("toponym_id", h["_id"]),
                "name": src.get("name", "?"),
                "lang": src.get("lang", "?"),
                "script": src.get("script", "?"),
                "embedding": emb
            })

    print(f"\nToponyms with valid embeddings: {len(toponyms)}")
    print("\nSample toponyms:")
    for t in toponyms[:20]:
        print(f"  {t['name']:25} ({t['lang']:5}, {t['script']:10})")
    if len(toponyms) > 20:
        print(f"  ... and {len(toponyms) - 20} more")

    # Build embedding matrix
    ids = [t["id"] for t in toponyms]
    vectors = np.array([t["embedding"] for t in toponyms])

    print(f"\nEmbedding matrix shape: {vectors.shape}")

    # Compute cosine distance matrix
    print("\nComputing cosine distance matrix...")
    distance_matrix = cosine_distances(vectors)

    # Show sample distances
    print("\nSample cosine distances (first 5x5):")
    print("          ", "  ".join(f"{toponyms[i]['name'][:8]:>8}" for i in range(min(5, len(toponyms)))))
    for i in range(min(5, len(toponyms))):
        row = "  ".join(f"{distance_matrix[i][j]:.3f}" for j in range(min(5, len(toponyms))))
        print(f"{toponyms[i]['name'][:10]:>10} {row}")

    # Run HDBSCAN
    print("\n" + "=" * 60)
    print("HDBSCAN CLUSTERING (min_cluster_size=2, min_samples=1)")
    print("=" * 60)

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=2,
        min_samples=1,
        metric='precomputed',
        cluster_selection_epsilon=0.0,
        allow_single_cluster=True
    )
    labels = clusterer.fit_predict(distance_matrix)

    # Group results
    clusters = defaultdict(list)
    for t, label in zip(toponyms, labels):
        clusters[int(label)].append(t)

    n_clusters = len([l for l in set(labels) if l >= 0])
    n_noise = list(labels).count(-1)

    print(f"\nResults: {n_clusters} clusters, {n_noise} noise points")

    print("\nClusters (showing name, lang, script):")
    for label in sorted(clusters.keys()):
        if label >= 0:
            print(f"\n  Cluster {label} ({len(clusters[label])} members):")
            for t in clusters[label][:10]:
                print(f"    {t['name']:25} ({t['lang']:5}, {t['script']})")
            if len(clusters[label]) > 10:
                print(f"    ... and {len(clusters[label]) - 10} more")

    if -1 in clusters:
        print(f"\n  Noise ({len(clusters[-1])} points):")
        for t in clusters[-1][:5]:
            print(f"    {t['name']:25} ({t['lang']:5}, {t['script']})")
        if len(clusters[-1]) > 5:
            print(f"    ... and {len(clusters[-1]) - 5} more")

    # Verify clustering makes sense
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    # Check if different scripts ended up in different clusters
    scripts_per_cluster = {}
    for label, members in clusters.items():
        if label >= 0:
            scripts = set(t['script'] for t in members)
            scripts_per_cluster[label] = scripts
            if len(scripts) > 1:
                print(f"  Cluster {label} has MULTIPLE scripts: {scripts}")
            else:
                print(f"  Cluster {label} is homogeneous: {scripts}")

    print("\n✓ HDBSCAN clustering test complete")

    # Also test a place we know should have multiple phonetic groups
    print("\n" + "=" * 60)
    print("BONUS: Testing on 'Moscow' (if available)")
    print("=" * 60)

    # Search for Moscow
    moscow_result = es.search(
        index="toponyms",
        size=100,
        query={
            "bool": {
                "must": [
                    {"match": {"name": "Moscow"}},
                    {"exists": {"field": "panphon_embedding"}}
                ]
            }
        },
        _source=["toponym_id", "name", "lang", "script", "panphon_embedding", "attestations"]
    )

    moscow_hits = moscow_result["hits"]["hits"]
    if moscow_hits:
        # Get the attestation (place_id) from the first hit
        moscow_place = moscow_hits[0]["_source"].get("attestations", [None])[0]
        if moscow_place:
            print(f"Found Moscow place: {moscow_place}")

            # Get all toponyms for Moscow
            moscow_toponyms_result = es.search(
                index="toponyms",
                size=100,
                query={
                    "bool": {
                        "must": [
                            {"term": {"attestations": moscow_place}},
                            {"exists": {"field": "panphon_embedding"}}
                        ]
                    }
                },
                _source=["name", "lang", "script", "panphon_embedding"]
            )

            moscow_toponyms = []
            for h in moscow_toponyms_result["hits"]["hits"]:
                src = h["_source"]
                emb = src.get("panphon_embedding", [])
                if emb:
                    moscow_toponyms.append({
                        "name": src.get("name", "?"),
                        "lang": src.get("lang", "?"),
                        "script": src.get("script", "?"),
                        "embedding": emb
                    })

            if len(moscow_toponyms) >= 3:
                print(f"\nMoscow has {len(moscow_toponyms)} toponyms with embeddings")
                for t in moscow_toponyms[:10]:
                    print(f"  {t['name']:20} ({t['lang']:5}, {t['script']})")

                vectors = np.array([t["embedding"] for t in moscow_toponyms])
                distance_matrix = cosine_distances(vectors)

                clusterer = hdbscan.HDBSCAN(
                    min_cluster_size=2,
                    min_samples=1,
                    metric='precomputed',
                    allow_single_cluster=True
                )
                labels = clusterer.fit_predict(distance_matrix)

                clusters = defaultdict(list)
                for t, label in zip(moscow_toponyms, labels):
                    clusters[int(label)].append(f"{t['name']} ({t['lang']}, {t['script']})")

                n_clusters = len([l for l in set(labels) if l >= 0])
                print(f"\nMoscow clustering: {n_clusters} clusters")
                for label in sorted(clusters.keys()):
                    if label >= 0:
                        print(f"  Cluster {label}: {clusters[label][:5]}")
    else:
        print("Moscow not found in index")


if __name__ == "__main__":
    main()

