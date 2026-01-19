#!/usr/bin/env python3
"""
Test HDBSCAN clustering on real toponyms from Elasticsearch.

Usage:

srun -p htc --mem=64G --cpus-per-task=4 --pty bash
cd /ix1/whcdh/elastic
python testing/test_hdbscan_clustering.py

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
from processing.settings import ES_HOST

def test_place_clustering(es, city_name):
    """Test HDBSCAN clustering for a specific city."""

    # Search for the city
    result = es.search(
        index="toponyms",
        size=100,
        query={
            "bool": {
                "must": [
                    {"match": {"name": city_name}},
                    {"exists": {"field": "panphon_embedding"}}
                ]
            }
        },
        _source=["toponym_id", "name", "lang", "script", "panphon_embedding", "attestations"]
    )

    hits = result["hits"]["hits"]
    if not hits:
        print(f"{city_name} not found in index")
        return

    # Get the attestation (place_id) from the first hit that matches the city name closely
    place_id = None
    for h in hits:
        src = h["_source"]
        if src.get("name", "").lower() == city_name.lower():
            attestations = src.get("attestations", [])
            if attestations:
                place_id = attestations[0]
                break

    if not place_id:
        # Fall back to first hit
        place_id = hits[0]["_source"].get("attestations", [None])[0]

    if not place_id:
        print(f"No place_id found for {city_name}")
        return

    print(f"Found {city_name} place: {place_id}")

    # Get all toponyms for this place
    toponyms_result = es.search(
        index="toponyms",
        size=200,
        query={
            "bool": {
                "must": [
                    {"term": {"attestations": place_id}},
                    {"exists": {"field": "panphon_embedding"}}
                ]
            }
        },
        _source=["name", "lang", "script", "panphon_embedding"]
    )

    toponyms = []
    for h in toponyms_result["hits"]["hits"]:
        src = h["_source"]
        emb = src.get("panphon_embedding", [])
        if emb:
            toponyms.append({
                "name": src.get("name", "?"),
                "lang": src.get("lang", "?"),
                "script": src.get("script", "?"),
                "embedding": emb
            })

    if len(toponyms) < 2:
        print(f"{city_name} has insufficient toponyms ({len(toponyms)})")
        return

    print(f"\n{city_name} has {len(toponyms)} toponyms with embeddings")

    # Show all toponyms grouped by script
    by_script = defaultdict(list)
    for t in toponyms:
        by_script[t['script']].append(t)

    print("\nToponyms by script:")
    for script, items in sorted(by_script.items()):
        print(f"  {script} ({len(items)}):")
        for t in items[:5]:
            print(f"    {t['name']:25} ({t['lang']})")
        if len(items) > 5:
            print(f"    ... and {len(items) - 5} more")

    if len(toponyms) < 3:
        # For 2 toponyms, just check similarity threshold
        vectors = np.array([t["embedding"] for t in toponyms])
        sim = 1 - cosine_distances(vectors)[0][1]
        print(f"\n2 toponyms - similarity: {sim:.4f}")
        if sim >= 0.5:
            print(f"  Would form positive pair: {toponyms[0]['name']} <-> {toponyms[1]['name']}")
        else:
            print(f"  Too dissimilar for pair")
        return

    # Run HDBSCAN clustering
    vectors = np.array([t["embedding"] for t in toponyms])
    distance_matrix = cosine_distances(vectors)

    # Use cluster_selection_epsilon=0.2 (cosine distance) to merge clusters
    # where members are within ~0.8 cosine similarity of each other.
    # This creates larger, more meaningful clusters for phonetically similar names.
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=2,
        min_samples=2,  # Better denoising
        metric='precomputed',
        cluster_selection_epsilon=0.2,  # Merge clusters within cosine distance 0.2 (sim >= 0.8)
        allow_single_cluster=True
    )
    labels = clusterer.fit_predict(distance_matrix)

    # Group results
    clusters = defaultdict(list)
    for t, label in zip(toponyms, labels):
        clusters[int(label)].append(t)

    n_clusters = len([l for l in set(labels) if l >= 0])
    n_noise = list(labels).count(-1)

    print(f"\nClustering results: {n_clusters} clusters, {n_noise} noise points")

    # Show clusters with details
    for label in sorted(clusters.keys()):
        if label >= 0:
            members = clusters[label]
            scripts = set(t['script'] for t in members)
            langs = set(t['lang'] for t in members)

            script_str = "/".join(sorted(scripts))
            print(f"\n  Cluster {label} ({len(members)} members, scripts: {script_str}):")
            for t in members[:8]:
                print(f"    {t['name']:25} ({t['lang']:5}, {t['script']})")
            if len(members) > 8:
                print(f"    ... and {len(members) - 8} more")

            # Calculate intra-cluster similarity
            if len(members) >= 2:
                member_indices = [i for i, (t, l) in enumerate(zip(toponyms, labels)) if l == label]
                intra_dists = []
                for i, idx1 in enumerate(member_indices):
                    for idx2 in member_indices[i+1:]:
                        intra_dists.append(1 - distance_matrix[idx1][idx2])
                if intra_dists:
                    print(f"    Intra-cluster similarity: {np.mean(intra_dists):.3f} (min: {min(intra_dists):.3f}, max: {max(intra_dists):.3f})")

    if -1 in clusters:
        print(f"\n  Noise ({len(clusters[-1])} points):")
        for t in clusters[-1][:5]:
            print(f"    {t['name']:25} ({t['lang']:5}, {t['script']})")
        if len(clusters[-1]) > 5:
            print(f"    ... and {len(clusters[-1]) - 5} more")

    # Summary: would generate pairs
    total_pairs = 0
    for label, members in clusters.items():
        if label >= 0 and len(members) >= 2:
            n = len(members)
            pairs = n * (n - 1) // 2
            total_pairs += pairs

    print(f"\n  Total positive pairs that would be generated: {total_pairs}")


def test_missing_scripts_dropout(es, missing_scripts):
    """
    Diagnose why certain scripts are not generating pairs.
    Tests for KNN and HDBSCAN dropout issues.
    """
    print("\n" + "=" * 60)
    print("MISSING SCRIPTS DROPOUT DIAGNOSTIC")
    print("=" * 60)

    # Scripts that use AnyASCII romanization (but still get panphon_embedding from romanized IPA)
    ROMANIZED_SCRIPTS = {"CJK", "HIRAGANA", "KATAKANA", "HANGUL"}

    for script in missing_scripts:
        print(f"\n{'=' * 60}")
        print(f"ANALYZING: {script}")
        print("=" * 60)

        # Check if this is a romanized script
        if script in ROMANIZED_SCRIPTS:
            print(f"⚠ ROMANIZED SCRIPT: {script}")
            print(f"  Pipeline: AnyASCII → romanized text → IPA → PanPhon embedding")
            print(f"  These SHOULD have panphon_embedding (from romanized IPA)")
            print(f"  If missing from pairs, check pairing logic for romanized forms")

            # Check total count (romanized forms SHOULD have panphon_embedding from romanized IPA)
            query = {"query": {"term": {"script": script}}}
            try:
                total_count = es.count(index="toponyms", body=query)["count"]
                print(f"  Total toponyms: {total_count:,}")

                if total_count == 0:
                    print(f"    ✗ No toponyms in index for this script")
                    continue

                # Check how many have panphon_embedding
                with_embedding_query = {
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"script": script}},
                                {"exists": {"field": "panphon_embedding"}}
                            ]
                        }
                    }
                }
                embedding_count = es.count(index="toponyms", body=with_embedding_query)["count"]
                print(f"  With panphon_embedding: {embedding_count:,} ({100*embedding_count/total_count:.1f}%)")

                if embedding_count == 0:
                    print(f"    ✗ No embeddings despite romanization - UPSTREAM ISSUE")
                    continue

                # For romanized scripts, check if they appear in multi-toponym places
                sample = es.search(
                    index="toponyms",
                    body={
                        "query": query["query"],
                        "size": 50,
                        "_source": ["attestations"]
                    }
                )

                place_ids = set()
                for h in sample["hits"]["hits"]:
                    attestations = h["_source"].get("attestations", [])
                    place_ids.update(attestations)

                if place_ids:
                    # Check if any of these places have multiple toponyms
                    multi_toponym_places = 0
                    for place_id in list(place_ids)[:20]:
                        place_result = es.count(
                            index="toponyms",
                            body={"query": {"term": {"attestations": place_id}}}
                        )
                        if place_result["count"] >= 2:
                            multi_toponym_places += 1

                    print(f"  Places with ≥2 toponyms: {multi_toponym_places}/20 sampled")
                    if multi_toponym_places == 0:
                        print(f"    ✗ Data sparsity: {script} toponyms only appear in isolation")
                    else:
                        print(f"    ℹ Potential for pairs exists (via romanized character matching)")
                        print(f"    ⚠ But missing from pairs → check character-level pairing logic")

            except Exception as e:
                print(f"  ERROR checking {script}: {e}")

            continue

        # For non-romanized scripts, check for panphon_embedding
        # 1. Check if we have any toponyms with embeddings for this script
        count_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"script": script}},
                        {"exists": {"field": "panphon_embedding"}}
                    ]
                }
            }
        }

        count = es.count(index="toponyms", body=count_query)["count"]
        print(f"Toponyms with panphon_embedding: {count:,}")

        if count == 0:
            print(f"  ✗ No toponyms with embeddings - UPSTREAM ISSUE (Epitran/PanPhon)")
            continue

        # 2. Get sample toponyms
        sample = es.search(
            index="toponyms",
            body={
                "query": count_query["query"],
                "size": 100,
                "_source": ["name", "lang", "script", "panphon_embedding", "attestations"]
            }
        )

        hits = sample["hits"]["hits"]
        print(f"Sample size: {len(hits)}")

        # 3. Group by place to see if multi-toponym places exist
        # For cross-script training, we need places with:
        # - At least 1 toponym in the target script (already filtered by query)
        # - At least 1 other toponym (in ANY script, including same script)
        place_to_toponyms_this_script = defaultdict(list)
        place_ids_with_this_script = set()

        for h in hits:
            src = h["_source"]
            attestations = src.get("attestations", [])
            for place_id in attestations:
                place_ids_with_this_script.add(place_id)
                place_to_toponyms_this_script[place_id].append({
                    "name": src["name"],
                    "lang": src.get("lang", "?"),
                    "script": src["script"],
                    "embedding": src["panphon_embedding"]
                })

        print(f"Places with at least 1 {script} toponym: {len(place_ids_with_this_script)}")

        # Now check if any of these places have OTHER toponyms (for cross-script pairing)
        places_with_multiple_toponyms = {}
        for place_id in list(place_ids_with_this_script)[:50]:  # Check first 50
            # Get ALL toponyms for this place (any script)
            all_place_toponyms = es.search(
                index="toponyms",
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"attestations": place_id}},
                                {"exists": {"field": "panphon_embedding"}}
                            ]
                        }
                    },
                    "size": 50,
                    "_source": ["name", "lang", "script", "panphon_embedding"]
                }
            )

            all_toponyms = []
            for h in all_place_toponyms["hits"]["hits"]:
                src = h["_source"]
                all_toponyms.append({
                    "name": src["name"],
                    "lang": src.get("lang", "?"),
                    "script": src["script"],
                    "embedding": src["panphon_embedding"]
                })

            if len(all_toponyms) >= 2:
                places_with_multiple_toponyms[place_id] = all_toponyms

        print(f"Places with ≥2 toponyms total (including {script}): {len(places_with_multiple_toponyms)}")

        if len(places_with_multiple_toponyms) == 0:
            print(f"  ✗ No multi-toponym places - CANNOT FORM PAIRS")
            print(f"     (Need places with {script} + at least 1 other toponym)")
            continue

        # 4. Test HDBSCAN on a sample multi-toponym place
        sample_place_id = list(places_with_multiple_toponyms.keys())[0]
        sample_toponyms = places_with_multiple_toponyms[sample_place_id]

        # Count how many are in the target script vs others
        target_script_count = sum(1 for t in sample_toponyms if t['script'] == script)
        other_script_count = len(sample_toponyms) - target_script_count
        scripts_present = set(t['script'] for t in sample_toponyms)

        print(f"\nSample place {sample_place_id} has {len(sample_toponyms)} toponyms:")
        print(f"  {script}: {target_script_count}, Other scripts: {other_script_count}")
        print(f"  Scripts: {', '.join(sorted(scripts_present))}")
        for t in sample_toponyms[:5]:
            print(f"  {t['name']:25} ({t['lang']:5}, {t['script']})")
        if len(sample_toponyms) > 5:
            print(f"  ... and {len(sample_toponyms) - 5} more")

        if len(sample_toponyms) == 2:
            # Test similarity threshold
            vectors = np.array([t["embedding"] for t in sample_toponyms])
            dist = cosine_distances(vectors)[0][1]
            sim = 1 - dist
            print(f"\n2-toponym similarity: {sim:.4f}")
            print(f"  Distance: {dist:.4f}")
            if sim >= 0.5:
                print(f"  ✓ Would form positive pair (threshold >= 0.5)")
            else:
                print(f"  ✗ DROPPED: Similarity < 0.5 threshold")
                print(f"     Reason: PanPhon embeddings may be too dissimilar for this script")

        elif len(sample_toponyms) >= 3:
            # Test HDBSCAN clustering
            vectors = np.array([t["embedding"] for t in sample_toponyms])
            distance_matrix = cosine_distances(vectors)

            # Show pairwise similarities
            print(f"\nPairwise similarities:")
            for i in range(len(sample_toponyms)):
                for j in range(i+1, len(sample_toponyms)):
                    sim = 1 - distance_matrix[i][j]
                    print(f"  {sample_toponyms[i]['name'][:15]:15} <-> {sample_toponyms[j]['name'][:15]:15}: {sim:.4f}")

            # Run HDBSCAN
            try:
                clusterer = hdbscan.HDBSCAN(
                    min_cluster_size=2,
                    min_samples=2,
                    metric='precomputed',
                    cluster_selection_epsilon=0.2,
                    allow_single_cluster=True
                )
                labels = clusterer.fit_predict(distance_matrix)

                n_clusters = len([l for l in set(labels) if l >= 0])
                n_noise = list(labels).count(-1)

                print(f"\nHDBSCAN results: {n_clusters} clusters, {n_noise} noise")

                if n_clusters == 0:
                    print(f"  ✗ HDBSCAN DROPOUT: All points classified as noise")
                    print(f"     Possible reasons:")
                    print(f"       - Embeddings too dissimilar (check similarities above)")
                    print(f"       - min_cluster_size=2 may be too strict")
                    print(f"       - cluster_selection_epsilon=0.2 may be too tight")
                else:
                    clusters = defaultdict(list)
                    for t, label in zip(sample_toponyms, labels):
                        clusters[label].append(t)

                    for label, members in sorted(clusters.items()):
                        if label >= 0:
                            print(f"  Cluster {label}: {len(members)} members")
                            for t in members:
                                print(f"    {t['name']}")
                        else:
                            print(f"  Noise: {len(members)} points")
                            for t in members:
                                print(f"    {t['name']}")

            except Exception as e:
                print(f"  ✗ HDBSCAN failed: {e}")

        # 5. Check cross-script potential
        print(f"\nChecking cross-script pairing potential...")
        # Find places that have BOTH this script AND another script
        sample_place_ids = list(places_with_multiple_toponyms.keys())[:10]
        cross_script_count = 0

        for pid in sample_place_ids:
            place_toponyms = es.search(
                index="toponyms",
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"attestations": pid}},
                                {"exists": {"field": "panphon_embedding"}}
                            ]
                        }
                    },
                    "size": 50,
                    "_source": ["script"]
                }
            )

            scripts = set(h["_source"]["script"] for h in place_toponyms["hits"]["hits"])
            if len(scripts) > 1 and script in scripts:
                cross_script_count += 1

        print(f"Places with {script} AND other scripts: {cross_script_count}/{len(sample_place_ids)}")

        if cross_script_count == 0:
            print(f"  ⚠ Low cross-script diversity - may limit training pairs")


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

    # TEST MISSING SCRIPTS FIRST
    missing_scripts = ["ARMENIAN", "GREEK", "GUJARATI", "HEBREW", "HIRAGANA", "KANNADA", "KATAKANA", "OTHER"]
    print(f"\nTesting {len(missing_scripts)} scripts that are missing from training pairs...")
    test_missing_scripts_dropout(es, missing_scripts)

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
    print("HDBSCAN CLUSTERING (min_cluster_size=2, min_samples=2, epsilon=0.2)")
    print("=" * 60)

    # Use cluster_selection_epsilon=0.2 (cosine distance) to merge clusters
    # where members are within ~0.8 cosine similarity of each other.
    # This creates larger, more meaningful clusters for phonetically similar names.
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=2,
        min_samples=2,  # Better denoising
        metric='precomputed',
        cluster_selection_epsilon=0.2,  # Merge clusters within cosine distance 0.2 (sim >= 0.8)
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

    # Test specific well-known places
    test_cities = ["London", "Moscow", "Beijing", "Paris"]

    for city_name in test_cities:
        print("\n" + "=" * 60)
        print(f"TESTING: {city_name}")
        print("=" * 60)

        test_place_clustering(es, city_name)




if __name__ == "__main__":
    main()

