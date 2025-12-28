#!/usr/bin/env python3
"""
Restructure HDF5 training data for optimized training across all phases.

Creates optimized HDF5 files with:
- Phase 1: Pre-computed triplets, packed features (already done by restructure_hdf5.py)
- Phase 2: Packed features, pre-encoded char sequences
- Phase 3: Pre-computed hard negative triplets (Stage A), packed text data

Usage:
    python restructure_all_phases.py input.h5 output_prefix [--seed 42]

    Creates:
      - {output_prefix}_phase1.h5  (if not exists)
      - {output_prefix}_phase2.h5
      - {output_prefix}_phase3.h5

Example:
    python restructure_all_phases.py training_data_gn.h5 training_data_gn_optimized
"""

import argparse
import os
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

import h5py
import numpy as np
from tqdm import tqdm


def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def build_char_vocab(romanized_list: List[str]) -> Tuple[Dict[str, int], int]:
    """Build character vocabulary from romanized strings."""
    chars = set()
    for rom in romanized_list:
        if isinstance(rom, bytes):
            rom = rom.decode('utf-8')
        chars.update(rom)

    # Reserve 0 for padding
    vocab = {'<PAD>': 0, '<UNK>': 1}
    for i, char in enumerate(sorted(chars), start=2):
        vocab[char] = i

    return vocab, len(vocab)


def encode_romanized(text: str, vocab: Dict[str, int], max_len: int) -> np.ndarray:
    """Encode romanized text to integer sequence."""
    if isinstance(text, bytes):
        text = text.decode('utf-8')

    ids = np.zeros(max_len, dtype=np.int16)
    for i, char in enumerate(text[:max_len]):
        ids[i] = vocab.get(char, vocab['<UNK>'])
    return ids


def build_lang_vocab(lang_list: List[str]) -> Dict[str, int]:
    """Build language vocabulary."""
    langs = set()
    for lang in lang_list:
        if isinstance(lang, bytes):
            lang = lang.decode('utf-8')
        langs.add(lang)

    return {lang: i for i, lang in enumerate(sorted(langs))}


def restructure_phase2(input_path: str, output_path: str, seed: int = 42):
    """
    Create optimized HDF5 for Phase 2 (Student-Teacher alignment).

    Output layout:
        /items/char_ids -> (N, max_romanized_len) pre-encoded characters
        /items/char_lengths -> (N,) length of each romanized string
        /items/lang_ids -> (N,) language indices
        /features/data -> (N, max_feat_len, 24) packed features
        /features/lengths -> (N,) feature sequence lengths
        /vocab/char_vocab -> character vocabulary mapping
        /vocab/lang_vocab -> language vocabulary mapping
    """
    print(f"\n{'=' * 60}")
    print("RESTRUCTURING FOR PHASE 2")
    print(f"{'=' * 60}")

    rng = np.random.default_rng(seed)

    print(f"Reading {input_path}...")

    with h5py.File(input_path, 'r') as f_in:
        total_items = f_in.attrs['total_items']
        print(f"  Total items: {total_items:,}")

        items = f_in['items']
        features_group = f_in['features']
        valid_feature_keys = set(features_group.keys())

        # First pass: identify valid items and collect metadata
        print("Scanning items...")
        valid_indices = []
        romanized_list = []
        lang_list = []
        feature_lengths = {}
        max_feature_len = 0
        max_romanized_len = 0

        for idx in tqdm(range(total_items), desc="Scanning"):
            feature_key = str(idx)
            if feature_key in valid_feature_keys:
                feat_shape = features_group[feature_key].shape
                if feat_shape[0] > 0:
                    valid_indices.append(idx)

                    rom = items['romanized'][idx]
                    if isinstance(rom, bytes):
                        rom = rom.decode('utf-8')
                    romanized_list.append(rom)
                    max_romanized_len = max(max_romanized_len, len(rom))

                    lang = items['lang'][idx]
                    if isinstance(lang, bytes):
                        lang = lang.decode('utf-8')
                    lang_list.append(lang)

                    feature_lengths[idx] = feat_shape[0]
                    max_feature_len = max(max_feature_len, feat_shape[0])

        n_valid = len(valid_indices)
        print(f"  Valid items: {n_valid:,}")
        print(f"  Max romanized length: {max_romanized_len}")
        print(f"  Max feature length: {max_feature_len}")

        # Build vocabularies
        print("Building vocabularies...")
        char_vocab, char_vocab_size = build_char_vocab(romanized_list)
        lang_vocab = build_lang_vocab(lang_list)
        print(f"  Char vocab size: {char_vocab_size}")
        print(f"  Lang vocab size: {len(lang_vocab)}")

        # Encode all romanized strings
        print("Encoding romanized strings...")
        char_ids = np.zeros((n_valid, max_romanized_len), dtype=np.int16)
        char_lengths = np.zeros(n_valid, dtype=np.int16)
        lang_ids = np.zeros(n_valid, dtype=np.int16)

        for new_idx, (rom, lang) in enumerate(tqdm(zip(romanized_list, lang_list),
                                                   total=n_valid, desc="Encoding")):
            char_ids[new_idx] = encode_romanized(rom, char_vocab, max_romanized_len)
            char_lengths[new_idx] = len(rom)
            lang_ids[new_idx] = lang_vocab[lang]

        # Write output
        print(f"Writing {output_path}...")

        with h5py.File(output_path, 'w') as f_out:
            # Attributes
            f_out.attrs['total_items'] = n_valid
            f_out.attrs['max_romanized_len'] = max_romanized_len
            f_out.attrs['max_feature_len'] = max_feature_len
            f_out.attrs['char_vocab_size'] = char_vocab_size
            f_out.attrs['lang_vocab_size'] = len(lang_vocab)
            f_out.attrs['seed'] = seed

            # Character data
            items_out = f_out.create_group('items')
            items_out.create_dataset('char_ids', data=char_ids, dtype=np.int16)
            items_out.create_dataset('char_lengths', data=char_lengths, dtype=np.int16)
            items_out.create_dataset('lang_ids', data=lang_ids, dtype=np.int16)

            # Vocabularies (store as attributes for simplicity)
            vocab_group = f_out.create_group('vocab')

            # Store char vocab
            char_vocab_data = np.array([(k, v) for k, v in char_vocab.items()],
                                       dtype=[('char', 'U10'), ('id', 'i4')])
            vocab_group.create_dataset('char_vocab', data=char_vocab_data)

            # Store lang vocab
            lang_vocab_data = np.array([(k, v) for k, v in lang_vocab.items()],
                                       dtype=[('lang', 'U10'), ('id', 'i4')])
            vocab_group.create_dataset('lang_vocab', data=lang_vocab_data)

            # Features - write in chunks
            features_out = f_out.create_group('features')
            feat_lengths = np.zeros(n_valid, dtype=np.int16)

            chunk_size = min(1000, n_valid)
            feat_dataset = features_out.create_dataset(
                'data',
                shape=(n_valid, max_feature_len, 24),
                dtype=np.float32,
                chunks=(chunk_size, max_feature_len, 24),
                compression='gzip',
                compression_opts=1
            )

            write_chunk_size = 10000
            print("Writing features...")

            for chunk_start in tqdm(range(0, n_valid, write_chunk_size), desc="Writing features"):
                chunk_end = min(chunk_start + write_chunk_size, n_valid)
                chunk_len = chunk_end - chunk_start

                chunk_buffer = np.zeros((chunk_len, max_feature_len, 24), dtype=np.float32)

                for i, new_idx in enumerate(range(chunk_start, chunk_end)):
                    old_idx = valid_indices[new_idx]
                    feature_key = str(old_idx)
                    feat = features_group[feature_key][:]
                    length = feat.shape[0]
                    chunk_buffer[i, :length, :] = feat
                    feat_lengths[new_idx] = length

                feat_dataset[chunk_start:chunk_end] = chunk_buffer

            features_out.create_dataset('lengths', data=feat_lengths, dtype=np.int16)

    print(f"Phase 2 restructuring complete: {output_path}")
    return char_vocab, lang_vocab


def restructure_phase3(input_path: str, output_path: str,
                       char_vocab: Dict[str, int] = None,
                       lang_vocab: Dict[str, int] = None,
                       seed: int = 42,
                       edit_distance_max: int = 3,
                       phonetic_distance_min: float = 0.3):
    """
    Create optimized HDF5 for Phase 3 (curriculum hard negatives).

    Pre-computes Stage A hard negatives:
    - Orthographically close (edit distance <= threshold)
    - Phonetically distant (IPA edit distance ratio >= threshold)

    Output layout:
        /triplets/anchor_idx, positive_idx, negative_idx -> pre-computed hard triplets
        /items/char_ids -> (N, max_len) pre-encoded characters
        /items/char_lengths -> (N,) lengths
        /items/lang_ids -> (N,) language indices
        /items/romanized -> (N,) original romanized strings (for fallback)
    """
    print(f"\n{'=' * 60}")
    print("RESTRUCTURING FOR PHASE 3 (Stage A Hard Negatives)")
    print(f"{'=' * 60}")

    rng = np.random.default_rng(seed)

    print(f"Reading {input_path}...")
    print(f"  Edit distance max: {edit_distance_max}")
    print(f"  Phonetic distance min: {phonetic_distance_min}")

    with h5py.File(input_path, 'r') as f_in:
        total_items = f_in.attrs['total_items']
        total_pairs = f_in.attrs['pairs_with_phonetic']

        print(f"  Total items: {total_items:,}")
        print(f"  Total pairs: {total_pairs:,}")

        items = f_in['items']
        pairs = f_in['pairs_with_phonetic']

        # Load all item data into memory for fast access
        print("Loading item data...")

        romanized_all = []
        lang_all = []
        ipa_all = []
        cluster_all = []

        for idx in tqdm(range(total_items), desc="Loading items"):
            rom = items['romanized'][idx]
            if isinstance(rom, bytes):
                rom = rom.decode('utf-8')
            romanized_all.append(rom)

            lang = items['lang'][idx]
            if isinstance(lang, bytes):
                lang = lang.decode('utf-8')
            lang_all.append(lang)

            ipa = items['ipa'][idx]
            if isinstance(ipa, bytes):
                ipa = ipa.decode('utf-8')
            ipa_all.append(ipa)

            cluster_all.append(int(items['cluster_id'][idx]))

        # Build vocabularies if not provided
        if char_vocab is None:
            print("Building char vocabulary...")
            char_vocab, _ = build_char_vocab(romanized_all)

        if lang_vocab is None:
            print("Building lang vocabulary...")
            lang_vocab = build_lang_vocab(lang_all)

        # Build first-char index for efficient candidate lookup
        print("Building first-char index...")
        first_char_index = defaultdict(list)
        for idx, rom in enumerate(romanized_all):
            if rom:
                first_char_index[rom[0].lower()].append(idx)

        # Build cluster membership
        cluster_to_items = defaultdict(set)
        for idx, cluster in enumerate(cluster_all):
            cluster_to_items[cluster].add(idx)

        n_clusters = len(cluster_to_items)
        print(f"  Clusters: {n_clusters}")

        # Pre-compute hard negative triplets
        print("Pre-computing Stage A hard negatives...")

        triplet_anchors = []
        triplet_positives = []
        triplet_negatives = []

        hard_neg_found = 0
        random_fallback = 0

        all_indices = list(range(total_items))

        for pair_idx in tqdm(range(total_pairs), desc="Mining hard negatives"):
            anchor_idx = int(pairs['anchor_idx'][pair_idx])
            positive_idx = int(pairs['positive_idx'][pair_idx])

            anchor_rom = romanized_all[anchor_idx]
            anchor_ipa = ipa_all[anchor_idx]
            anchor_cluster = cluster_all[anchor_idx]

            negative_idx = None

            if anchor_rom:
                # Get candidates with similar first character
                first_char = anchor_rom[0].lower()
                candidates = list(first_char_index.get(first_char, []))

                # Expand to adjacent characters if not enough candidates
                if len(candidates) < 20:
                    for offset in [-1, 1, -2, 2]:
                        adj_char = chr(ord(first_char) + offset)
                        candidates.extend(first_char_index.get(adj_char, []))

                if len(candidates) >= 5:
                    rng.shuffle(candidates)

                    best_neg_idx = None
                    best_score = -float('inf')

                    for neg_idx in candidates[:100]:  # Check up to 100 candidates
                        # Must be different cluster
                        if cluster_all[neg_idx] == anchor_cluster:
                            continue

                        neg_rom = romanized_all[neg_idx]
                        if not neg_rom:
                            continue

                        # Check edit distance
                        edit_dist = levenshtein_distance(anchor_rom.lower(), neg_rom.lower())
                        if edit_dist > edit_distance_max:
                            continue

                        # Check phonetic distance
                        neg_ipa = ipa_all[neg_idx]
                        if anchor_ipa and neg_ipa:
                            ipa_edit = levenshtein_distance(anchor_ipa, neg_ipa)
                            max_ipa_len = max(len(anchor_ipa), len(neg_ipa), 1)
                            phon_dist = ipa_edit / max_ipa_len

                            if phon_dist < phonetic_distance_min:
                                continue

                            score = phon_dist - (edit_dist * 0.1)
                        else:
                            score = -edit_dist

                        if score > best_score:
                            best_score = score
                            best_neg_idx = neg_idx

                    if best_neg_idx is not None:
                        negative_idx = best_neg_idx
                        hard_neg_found += 1

            # Fallback to random negative
            if negative_idx is None:
                if n_clusters > 1:
                    other_clusters = [c for c in cluster_to_items.keys() if c != anchor_cluster]
                    if other_clusters:
                        neg_cluster = rng.choice(other_clusters)
                        neg_candidates = list(cluster_to_items[neg_cluster])
                        negative_idx = neg_candidates[rng.integers(len(neg_candidates))]

                if negative_idx is None:
                    negative_idx = all_indices[rng.integers(total_items)]
                    while negative_idx == anchor_idx or negative_idx == positive_idx:
                        negative_idx = all_indices[rng.integers(total_items)]

                random_fallback += 1

            triplet_anchors.append(anchor_idx)
            triplet_positives.append(positive_idx)
            triplet_negatives.append(negative_idx)

        print(f"  Hard negatives found: {hard_neg_found:,} ({100 * hard_neg_found / total_pairs:.1f}%)")
        print(f"  Random fallback: {random_fallback:,} ({100 * random_fallback / total_pairs:.1f}%)")

        # Encode romanized strings
        print("Encoding romanized strings...")
        max_romanized_len = max(len(rom) for rom in romanized_all)

        char_ids = np.zeros((total_items, max_romanized_len), dtype=np.int16)
        char_lengths = np.zeros(total_items, dtype=np.int16)
        lang_ids = np.zeros(total_items, dtype=np.int16)

        for idx, (rom, lang) in enumerate(tqdm(zip(romanized_all, lang_all),
                                               total=total_items, desc="Encoding")):
            char_ids[idx] = encode_romanized(rom, char_vocab, max_romanized_len)
            char_lengths[idx] = len(rom)
            lang_ids[idx] = lang_vocab.get(lang, 0)

        # Write output
        print(f"Writing {output_path}...")

        with h5py.File(output_path, 'w') as f_out:
            # Attributes
            f_out.attrs['total_items'] = total_items
            f_out.attrs['total_triplets'] = len(triplet_anchors)
            f_out.attrs['max_romanized_len'] = max_romanized_len
            f_out.attrs['char_vocab_size'] = len(char_vocab)
            f_out.attrs['lang_vocab_size'] = len(lang_vocab)
            f_out.attrs['hard_negatives_found'] = hard_neg_found
            f_out.attrs['random_fallback'] = random_fallback
            f_out.attrs['edit_distance_max'] = edit_distance_max
            f_out.attrs['phonetic_distance_min'] = phonetic_distance_min
            f_out.attrs['seed'] = seed

            # Triplets
            triplets = f_out.create_group('triplets')
            triplets.create_dataset('anchor_idx', data=np.array(triplet_anchors, dtype=np.int32))
            triplets.create_dataset('positive_idx', data=np.array(triplet_positives, dtype=np.int32))
            triplets.create_dataset('negative_idx', data=np.array(triplet_negatives, dtype=np.int32))

            # Items
            items_out = f_out.create_group('items')
            items_out.create_dataset('char_ids', data=char_ids, dtype=np.int16)
            items_out.create_dataset('char_lengths', data=char_lengths, dtype=np.int16)
            items_out.create_dataset('lang_ids', data=lang_ids, dtype=np.int16)

            # Store romanized for debugging/fallback
            dt_str = h5py.special_dtype(vlen=str)
            items_out.create_dataset('romanized', data=romanized_all, dtype=dt_str)

            # Vocabularies
            vocab_group = f_out.create_group('vocab')
            char_vocab_data = np.array([(k, v) for k, v in char_vocab.items()],
                                       dtype=[('char', 'U10'), ('id', 'i4')])
            vocab_group.create_dataset('char_vocab', data=char_vocab_data)

            lang_vocab_data = np.array([(k, v) for k, v in lang_vocab.items()],
                                       dtype=[('lang', 'U10'), ('id', 'i4')])
            vocab_group.create_dataset('lang_vocab', data=lang_vocab_data)

    print(f"Phase 3 restructuring complete: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Restructure HDF5 training data for optimized multi-phase training"
    )
    parser.add_argument('input', help='Input HDF5 file')
    parser.add_argument('output_prefix', help='Output prefix (creates _phase2.h5 and _phase3.h5)')
    parser.add_argument('--seed', type=int, default=42, help='Random seed')
    parser.add_argument('--phase2-only', action='store_true', help='Only create Phase 2 file')
    parser.add_argument('--phase3-only', action='store_true', help='Only create Phase 3 file')
    parser.add_argument('--edit-distance-max', type=int, default=3,
                        help='Max edit distance for Stage A hard negatives')
    parser.add_argument('--phonetic-distance-min', type=float, default=0.3,
                        help='Min phonetic distance for Stage A hard negatives')

    args = parser.parse_args()

    phase2_path = f"{args.output_prefix}_phase2.h5"
    phase3_path = f"{args.output_prefix}_phase3.h5"

    char_vocab = None
    lang_vocab = None

    if not args.phase3_only:
        char_vocab, lang_vocab = restructure_phase2(
            args.input, phase2_path, seed=args.seed
        )

    if not args.phase2_only:
        restructure_phase3(
            args.input, phase3_path,
            char_vocab=char_vocab,
            lang_vocab=lang_vocab,
            seed=args.seed,
            edit_distance_max=args.edit_distance_max,
            phonetic_distance_min=args.phonetic_distance_min
        )

    print("\n" + "=" * 60)
    print("ALL RESTRUCTURING COMPLETE")
    print("=" * 60)

    for path in [phase2_path, phase3_path]:
        if os.path.exists(path):
            size_gb = os.path.getsize(path) / (1024 ** 3)
            print(f"  {path}: {size_gb:.2f} GB")


if __name__ == '__main__':
    main()