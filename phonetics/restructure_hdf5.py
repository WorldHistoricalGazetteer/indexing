#!/usr/bin/env python3
"""
Restructure HDF5 training data for optimized Phase 1 training.

Transforms the original HDF5 format into an optimized layout:
- Pre-generates all (anchor, positive, negative) triplets
- Packs variable-length features into contiguous arrays with length indices
- Eliminates runtime negative sampling and string key lookups

Usage:
    python restructure_hdf5.py input.h5 output.h5 [--seed 42]

The output file can be used with OptimizedPhase1Dataset for ~5-10x speedup.
"""

import argparse
import sys
from collections import defaultdict

import h5py
import numpy as np
from tqdm import tqdm


def restructure_hdf5(input_path: str, output_path: str, seed: int = 42):
    """
    Restructure HDF5 for optimized Phase 1 training.

    Input layout:
        /items/romanized, /items/cluster_id, /items/has_phonetic, ...
        /features/{item_idx} -> variable length (L, 24) arrays
        /pairs_with_phonetic/anchor_idx, positive_idx

    Output layout:
        /features/data -> (N_items, max_len, 24) contiguous array
        /features/lengths -> (N_items,) length of each feature sequence
        /triplets/anchor_idx, positive_idx, negative_idx -> pre-computed
        /items/* -> copied from input
    """
    rng = np.random.default_rng(seed)

    print(f"Reading {input_path}...")

    with h5py.File(input_path, 'r') as f_in:
        # --- Gather metadata ---
        total_items = f_in.attrs['total_items']
        total_pairs = f_in.attrs['pairs_with_phonetic']

        print(f"  Total items: {total_items:,}")
        print(f"  Total pairs: {total_pairs:,}")

        # --- Build cluster maps and identify items with valid features ---
        print("Building cluster maps...")
        items = f_in['items']
        features_group = f_in['features']

        cluster_to_items = defaultdict(list)
        item_has_features = {}
        feature_lengths = {}
        max_feature_len = 0

        valid_feature_keys = set(features_group.keys())

        for idx in tqdm(range(total_items), desc="Scanning items"):
            feature_key = str(idx)
            if feature_key in valid_feature_keys:
                feat = features_group[feature_key]
                if feat.shape[0] > 0:
                    item_has_features[idx] = True
                    feature_lengths[idx] = feat.shape[0]
                    max_feature_len = max(max_feature_len, feat.shape[0])

                    cluster_id = int(items['cluster_id'][idx])
                    cluster_to_items[cluster_id].append(idx)

        print(f"  Items with features: {len(item_has_features):,}")
        print(f"  Max feature length: {max_feature_len}")
        print(f"  Clusters: {len(cluster_to_items):,}")

        # --- Precompute cluster exclusion lists ---
        cluster_ids = list(cluster_to_items.keys())
        cluster_ids_excl = {
            c: [x for x in cluster_ids if x != c]
            for c in cluster_ids
        }

        # --- Pre-generate triplets ---
        print("Pre-generating triplets...")
        pairs = f_in['pairs_with_phonetic']

        triplet_anchors = []
        triplet_positives = []
        triplet_negatives = []

        skipped = 0
        for pair_idx in tqdm(range(total_pairs), desc="Generating triplets"):
            anchor_idx = int(pairs['anchor_idx'][pair_idx])
            positive_idx = int(pairs['positive_idx'][pair_idx])

            # Skip if anchor or positive missing features
            if anchor_idx not in item_has_features or positive_idx not in item_has_features:
                skipped += 1
                continue

            anchor_cluster = int(items['cluster_id'][anchor_idx])
            other_clusters = cluster_ids_excl.get(anchor_cluster, [])

            if other_clusters:
                neg_cluster = rng.choice(other_clusters)
                neg_candidates = cluster_to_items[neg_cluster]
                negative_idx = neg_candidates[rng.integers(len(neg_candidates))]
            else:
                # Fallback: sample any item except anchor/positive
                all_items = list(item_has_features.keys())
                available = [i for i in all_items if i != anchor_idx and i != positive_idx]
                if available:
                    negative_idx = rng.choice(available)
                else:
                    negative_idx = positive_idx  # Last resort

            triplet_anchors.append(anchor_idx)
            triplet_positives.append(positive_idx)
            triplet_negatives.append(negative_idx)

        print(f"  Valid triplets: {len(triplet_anchors):,}")
        print(f"  Skipped (missing features): {skipped:,}")

        # --- Create item index mapping (only items with features) ---
        # Map original item_idx -> new contiguous index
        valid_items = sorted(item_has_features.keys())
        old_to_new = {old_idx: new_idx for new_idx, old_idx in enumerate(valid_items)}

        # Remap triplet indices
        triplet_anchors = np.array([old_to_new[i] for i in triplet_anchors], dtype=np.int32)
        triplet_positives = np.array([old_to_new[i] for i in triplet_positives], dtype=np.int32)
        triplet_negatives = np.array([old_to_new[i] for i in triplet_negatives], dtype=np.int32)

        # --- Pack features into contiguous array ---
        print("Packing features into contiguous array...")
        n_valid = len(valid_items)
        feat_dim = 24  # Articulatory features

        # Allocate contiguous arrays
        packed_features = np.zeros((n_valid, max_feature_len, feat_dim), dtype=np.float32)
        packed_lengths = np.zeros(n_valid, dtype=np.int16)

        for new_idx, old_idx in enumerate(tqdm(valid_items, desc="Packing features")):
            feature_key = str(old_idx)
            feat = features_group[feature_key][:]
            length = feat.shape[0]
            packed_features[new_idx, :length, :] = feat
            packed_lengths[new_idx] = length

        # --- Copy item metadata (remapped) ---
        print("Preparing item metadata...")

        # We need to copy only the valid items' metadata
        romanized_data = []
        lang_data = []
        cluster_data = []

        for old_idx in valid_items:
            romanized_data.append(items['romanized'][old_idx])
            lang_data.append(items['lang'][old_idx])
            cluster_data.append(items['cluster_id'][old_idx])

        # --- Write output file ---
        print(f"Writing {output_path}...")

        with h5py.File(output_path, 'w') as f_out:
            # Attributes
            f_out.attrs['total_items'] = n_valid
            f_out.attrs['total_triplets'] = len(triplet_anchors)
            f_out.attrs['max_feature_length'] = max_feature_len
            f_out.attrs['feature_dim'] = feat_dim
            f_out.attrs['original_file'] = input_path
            f_out.attrs['seed'] = seed

            # Triplets (pre-generated)
            triplets = f_out.create_group('triplets')
            triplets.create_dataset('anchor_idx', data=triplet_anchors, dtype=np.int32)
            triplets.create_dataset('positive_idx', data=triplet_positives, dtype=np.int32)
            triplets.create_dataset('negative_idx', data=triplet_negatives, dtype=np.int32)

            # Features (contiguous)
            features = f_out.create_group('features')
            features.create_dataset(
                'data',
                data=packed_features,
                dtype=np.float32,
                chunks=(min(1000, n_valid), max_feature_len, feat_dim),
                compression='gzip',
                compression_opts=1  # Fast compression
            )
            features.create_dataset('lengths', data=packed_lengths, dtype=np.int16)

            # Item metadata (remapped)
            items_out = f_out.create_group('items')

            # Handle string datasets
            dt_str = h5py.special_dtype(vlen=str)
            items_out.create_dataset('romanized', data=romanized_data, dtype=dt_str)
            items_out.create_dataset('lang', data=lang_data, dtype=dt_str)
            items_out.create_dataset('cluster_id', data=np.array(cluster_data, dtype=np.int32))

    print("\nDone!")
    print(f"  Output: {output_path}")

    # Print size comparison
    import os
    input_size = os.path.getsize(input_path) / (1024 ** 3)
    output_size = os.path.getsize(output_path) / (1024 ** 3)
    print(f"  Input size:  {input_size:.2f} GB")
    print(f"  Output size: {output_size:.2f} GB")


def main():
    parser = argparse.ArgumentParser(
        description="Restructure HDF5 training data for optimized Phase 1 training"
    )
    parser.add_argument('input', help='Input HDF5 file')
    parser.add_argument('output', help='Output HDF5 file')
    parser.add_argument('--seed', type=int, default=42, help='Random seed for negative sampling')

    args = parser.parse_args()

    restructure_hdf5(args.input, args.output, args.seed)


if __name__ == '__main__':
    main()