"""
Training Functions for Phonetic Similarity Model.

Three-phase training pipeline:
- Phase 1: Train Teacher (phonetic encoder) with triplet loss
- Phase 2: Align Student to Teacher (MSE + cosine loss)
- Phase 3: Fine-tune Student with curriculum hard negatives

v2 Changes:
- Both encoders now use BiLSTM + Self-Attention + Attention-Aware Pooling
- Phase 3 supports staged curriculum hard negatives:
  - Stage A: Orthographically close, phonetically distant
  - Stage B: Model-mined false positives (optional second pass)

v3 Changes:
- Multi-source dataset support with oversampling
- Separate HDF5 files for different data sources (GeoNames, Index Villaris, Pleiades)
- Configurable oversampling multipliers per source
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from .config import Config
from .vocab import CharVocab, LangVocab
from .models import PhoneticEncoder, CharEncoder, HybridPhoneticModel
from .losses import TripletLoss, RobustAlignmentLoss
from .streaming_datasets import (
    MultiSourcePhase1Dataset,
    MultiSourcePhase2Dataset,
    MultiSourcePhase3Dataset,
)


# =============================================================================
# Multi-Source Dataset Configuration
# =============================================================================

@dataclass
class DataSource:
    """Configuration for a single training data source."""
    name: str
    path: str
    oversample: int = 1


# Default data sources (can be overridden in function calls)
# Historical sources are oversampled to achieve ~10% representation per epoch
DATA_SOURCES = [
    DataSource(
        name='GeoNames',
        path='/ix1/whcdh/models/phonetic/data/training_data_gn.h5',
        oversample=1,
    ),
    DataSource(
        name='Pleiades+IV',
        path='/ix1/whcdh/models/phonetic/data/training_data_pl,iv.h5',
        oversample=24,
    ),
]


# =============================================================================
# Collate Functions
# =============================================================================

def collate_phase1(batch: List[Dict]) -> Dict[str, Tuple[torch.Tensor, torch.Tensor]]:
    """Collate function for Phase 1 (phonetic features only)."""

    def pad_features(features_list):
        lengths = torch.tensor([len(f) for f in features_list])
        max_len = max(lengths)
        feat_dim = features_list[0].shape[1]
        padded = torch.zeros(len(features_list), max_len, feat_dim)
        for i, f in enumerate(features_list):
            padded[i, :len(f)] = f
        return padded, lengths

    anchor, anchor_len = pad_features([b['anchor_features'] for b in batch])
    positive, pos_len = pad_features([b['positive_features'] for b in batch])
    negative, neg_len = pad_features([b['negative_features'] for b in batch])

    return {
        'anchor': (anchor, anchor_len),
        'positive': (positive, pos_len),
        'negative': (negative, neg_len)
    }


def collate_phase2(batch: List[Dict]) -> Dict[str, torch.Tensor]:
    """Collate function for Phase 2 (alignment training)."""

    def pad_ids(ids_list):
        lengths = torch.tensor([len(ids) for ids in ids_list])
        max_len = max(lengths)
        padded = torch.zeros(len(ids_list), max_len, dtype=torch.long)
        for i, ids in enumerate(ids_list):
            padded[i, :len(ids)] = ids
        return padded, lengths

    def pad_features(features_list):
        lengths = torch.tensor([len(f) for f in features_list])
        max_len = max(lengths)
        feat_dim = features_list[0].shape[1]
        padded = torch.zeros(len(features_list), max_len, feat_dim)
        for i, f in enumerate(features_list):
            padded[i, :len(f)] = f
        return padded, lengths

    char_ids, char_lengths = pad_ids([b['char_ids'] for b in batch])
    lang_ids = torch.stack([b['lang_id'] for b in batch])
    phone_feats, phone_lengths = pad_features([b['phonetic_features'] for b in batch])

    return {
        'char_ids': char_ids,
        'char_lengths': char_lengths,
        'lang_ids': lang_ids,
        'phonetic_features': phone_feats,
        'phonetic_lengths': phone_lengths
    }


def collate_phase3(batch: List[Dict]) -> Dict[str, Tuple[torch.Tensor, ...]]:
    """Collate function for Phase 3 (character triplets)."""

    def pad_ids(ids_list):
        lengths = torch.tensor([len(ids) for ids in ids_list])
        max_len = max(lengths)
        padded = torch.zeros(len(ids_list), max_len, dtype=torch.long)
        for i, ids in enumerate(ids_list):
            padded[i, :len(ids)] = ids
        return padded, lengths

    anchor_ids, anchor_lens = pad_ids([b['anchor_char_ids'] for b in batch])
    anchor_langs = torch.stack([b['anchor_lang_id'] for b in batch])

    pos_ids, pos_lens = pad_ids([b['positive_char_ids'] for b in batch])
    pos_langs = torch.stack([b['positive_lang_id'] for b in batch])

    neg_ids, neg_lens = pad_ids([b['negative_char_ids'] for b in batch])
    neg_langs = torch.stack([b['negative_lang_id'] for b in batch])

    return {
        'anchor': (anchor_ids, anchor_langs, anchor_lens),
        'positive': (pos_ids, pos_langs, pos_lens),
        'negative': (neg_ids, neg_langs, neg_lens)
    }


# =============================================================================
# Training Functions
# =============================================================================

def train_phase1(
    sources: List[DataSource] = None,
    output_path: str = None,
    epochs: int = Config.PHASE1_EPOCHS,
    subsample_pairs: int = Config.SUBSAMPLE_PAIRS,
    batch_size: int = Config.BATCH_SIZE,
    lr: float = Config.LEARNING_RATE
) -> PhoneticEncoder:
    """
    Phase 1: Train phonetic encoder (Teacher).

    Uses BiLSTM + Self-Attention + Attention-Aware Pooling architecture.
    Trained with triplet loss on phonetically similar/dissimilar pairs.

    Args:
        sources: List of DataSource objects. Defaults to DATA_SOURCES.
        output_path: Where to save the trained model.
        epochs: Number of training epochs.
        subsample_pairs: Max pairs to sample per source.
        batch_size: Training batch size.
        lr: Learning rate.
    """
    # Handle defaults
    if sources is None:
        sources = DATA_SOURCES

    data_paths = [s.path for s in sources]
    oversample_factors = [s.oversample for s in sources]

    print("=" * 60)
    print("Phase 1: Training Phonetic Encoder (Teacher)")
    print("  Architecture: BiLSTM + Self-Attention + Attention Pooling")
    print("=" * 60)

    # Log data sources
    print("\nData sources:")
    for source in sources:
        print(f"  {source.name}: {source.path} (oversample: {source.oversample}x)")
    print()

    train_dataset = MultiSourcePhase1Dataset(
        data_paths, oversample_factors,
        split='train', subsample_pairs=subsample_pairs
    )
    val_dataset = MultiSourcePhase1Dataset(
        data_paths, oversample_factors,
        split='val', subsample_pairs=subsample_pairs
    )

    print(f"Training pairs: {len(train_dataset):,}")
    print(f"Validation pairs: {len(val_dataset):,}", flush=True)

    # Use num_workers=0 to avoid HDF5 multiprocessing issues
    # Each worker would need its own file handle, which complicates things
    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        collate_fn=collate_phase1, num_workers=0, pin_memory=True
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        collate_fn=collate_phase1, num_workers=0, pin_memory=True
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}", flush=True)

    model = PhoneticEncoder().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', patience=5, factor=0.5
    )
    criterion = TripletLoss()

    best_val_loss = float('inf')

    for epoch in range(epochs):
        # Training
        model.train()
        train_loss = 0
        num_batches = 0

        for batch_idx, batch in enumerate(train_loader):
            anchor_seq, anchor_len = batch['anchor']
            pos_seq, pos_len = batch['positive']
            neg_seq, neg_len = batch['negative']

            anchor_seq = anchor_seq.to(device)
            anchor_len = anchor_len.to(device)
            pos_seq = pos_seq.to(device)
            pos_len = pos_len.to(device)
            neg_seq = neg_seq.to(device)
            neg_len = neg_len.to(device)

            optimizer.zero_grad()

            anchor_emb = model(anchor_seq, anchor_len)
            pos_emb = model(pos_seq, pos_len)
            neg_emb = model(neg_seq, neg_len)

            loss = criterion(anchor_emb, pos_emb, neg_emb)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            train_loss += loss.item()
            num_batches += 1

            # Progress logging every 100 batches
            if (batch_idx + 1) % 100 == 0:
                avg_loss = train_loss / num_batches
                print(f"  Epoch {epoch+1} | Batch {batch_idx+1}/{len(train_loader)} | Loss: {avg_loss:.4f}", flush=True)

        # Validation
        model.eval()
        val_loss = 0
        val_batches = 0
        with torch.no_grad():
            for batch in val_loader:
                anchor_seq, anchor_len = batch['anchor']
                pos_seq, pos_len = batch['positive']
                neg_seq, neg_len = batch['negative']

                anchor_emb = model(anchor_seq.to(device), anchor_len.to(device))
                pos_emb = model(pos_seq.to(device), pos_len.to(device))
                neg_emb = model(neg_seq.to(device), neg_len.to(device))

                loss = criterion(anchor_emb, pos_emb, neg_emb)
                val_loss += loss.item()
                val_batches += 1

        train_loss /= num_batches
        val_loss /= val_batches
        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | Train: {train_loss:.4f} | Val: {val_loss:.4f}", flush=True)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save({
                'model_state': model.state_dict(),
                'epoch': epoch,
                'val_loss': val_loss
            }, output_path)
            print(f"  → Saved best model (val_loss: {val_loss:.4f})", flush=True)

    print(f"\nPhase 1 complete. Best model saved to {output_path}")
    return model


def train_phase2(
    sources: List[DataSource] = None,
    phase1_path: str = None,
    output_path: str = None,
    epochs: int = Config.PHASE2_EPOCHS,
    batch_size: int = Config.BATCH_SIZE,
    lr: float = Config.LEARNING_RATE
) -> Tuple[PhoneticEncoder, CharEncoder, CharVocab, LangVocab]:
    """
    Phase 2: Alignment training (Student → Teacher).

    Trains the character encoder (Student) to produce embeddings that
    match the phonetic encoder (Teacher) output.

    Args:
        sources: List of DataSource objects. Defaults to DATA_SOURCES.
        phase1_path: Path to Phase 1 trained model.
        output_path: Where to save the trained model.
        epochs: Number of training epochs.
        batch_size: Training batch size.
        lr: Learning rate.
    """
    # Handle defaults
    if sources is None:
        sources = DATA_SOURCES

    data_paths = [s.path for s in sources]
    oversample_factors = [s.oversample for s in sources]

    print("=" * 60)
    print("Phase 2: Alignment Training (Student → Teacher)")
    print("  Architecture: BiLSTM + Self-Attention + Attention Pooling")
    print("=" * 60)

    # Log data sources
    print("\nData sources:")
    for source in sources:
        print(f"  {source.name}: {source.path} (oversample: {source.oversample}x)")
    print()

    # Build vocabularies from all HDF5 files
    char_vocab = CharVocab(vocab_size=Config.VOCAB_SIZE)
    char_vocab.fit_multi(data_paths)

    lang_vocab = LangVocab()
    lang_vocab.fit_multi(data_paths)

    train_dataset = MultiSourcePhase2Dataset(
        data_paths, oversample_factors,
        char_vocab, lang_vocab, split='train'
    )
    val_dataset = MultiSourcePhase2Dataset(
        data_paths, oversample_factors,
        char_vocab, lang_vocab, split='val'
    )

    print(f"Training items: {len(train_dataset):,}", flush=True)
    print(f"Validation items: {len(val_dataset):,}", flush=True)

    # Use num_workers=0 to avoid HDF5 multiprocessing issues
    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        collate_fn=collate_phase2, num_workers=0, pin_memory=True
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        collate_fn=collate_phase2, num_workers=0, pin_memory=True
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}", flush=True)

    # Load pre-trained phonetic encoder (Teacher)
    phonetic_encoder = PhoneticEncoder().to(device)
    checkpoint = torch.load(phase1_path, map_location=device)
    phonetic_encoder.load_state_dict(checkpoint['model_state'])
    phonetic_encoder.eval()

    # Freeze Teacher
    for param in phonetic_encoder.parameters():
        param.requires_grad = False
    print("Teacher (phonetic encoder) frozen")

    # Create Student (character encoder)
    char_encoder = CharEncoder(
        vocab_size=char_vocab.vocab_size,
        num_langs=lang_vocab.next_id
    ).to(device)

    optimizer = torch.optim.Adam(char_encoder.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', patience=5, factor=0.5
    )
    criterion = RobustAlignmentLoss()

    best_val_loss = float('inf')

    for epoch in range(epochs):
        # Training
        char_encoder.train()
        train_loss = 0

        for batch in train_loader:
            char_ids = batch['char_ids'].to(device)
            char_lengths = batch['char_lengths'].to(device)
            lang_ids = batch['lang_ids'].to(device)
            phone_feats = batch['phonetic_features'].to(device)
            phone_lengths = batch['phonetic_lengths'].to(device)

            optimizer.zero_grad()

            # Get target from frozen Teacher
            with torch.no_grad():
                target_emb = phonetic_encoder(phone_feats, phone_lengths)

            # Train Student to match
            char_emb = char_encoder(char_ids, lang_ids, char_lengths)

            loss = criterion(char_emb, target_emb)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(char_encoder.parameters(), max_norm=1.0)
            optimizer.step()
            train_loss += loss.item()

        # Validation
        char_encoder.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                char_ids = batch['char_ids'].to(device)
                char_lengths = batch['char_lengths'].to(device)
                lang_ids = batch['lang_ids'].to(device)
                phone_feats = batch['phonetic_features'].to(device)
                phone_lengths = batch['phonetic_lengths'].to(device)

                target_emb = phonetic_encoder(phone_feats, phone_lengths)
                char_emb = char_encoder(char_ids, lang_ids, char_lengths)

                loss = criterion(char_emb, target_emb)
                val_loss += loss.item()

        train_loss /= len(train_loader)
        val_loss /= len(val_loader)
        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | Train: {train_loss:.4f} | Val: {val_loss:.4f}", flush=True)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save({
                'phonetic_state': phonetic_encoder.state_dict(),
                'char_state': char_encoder.state_dict(),
                'epoch': epoch,
                'val_loss': val_loss
            }, output_path)

            # Save vocabularies
            vocab_dir = os.path.dirname(output_path) or '.'
            base_name = os.path.splitext(os.path.basename(output_path))[0]
            char_vocab.save(os.path.join(vocab_dir, f'{base_name}_char_vocab.pkl'))
            lang_vocab.save(os.path.join(vocab_dir, f'{base_name}_lang_vocab.pkl'))

            print(f"  → Saved best model (val_loss: {val_loss:.4f})", flush=True)

    print(f"\nPhase 2 complete. Best model saved to {output_path}")
    return phonetic_encoder, char_encoder, char_vocab, lang_vocab


def train_phase3(
    sources: List[DataSource] = None,
    phase2_path: str = None,
    output_path: str = None,
    subsample_pairs: int = Config.SUBSAMPLE_PAIRS,
    epochs: int = Config.PHASE3_EPOCHS,
    batch_size: int = Config.BATCH_SIZE,
    lr: float = 5e-4,
    negative_stage: str = 'A'
) -> HybridPhoneticModel:
    """
    Phase 3: Fine-tune with curriculum hard negatives.

    CRITICAL: Each negative stage must REPLACE, not augment, the previous one.
    Do NOT mix negative types in the same training pass.

    Stage A (Default): Orthographically close, phonetically distant
        - Targets false friends and spelling-driven false positives
        - Use this EXCLUSIVELY for early Phase 3 fine-tuning

    Stage B (Optional): Model-mined false positives
        - Run AFTER initial Phase 3 pass
        - Uses model's own failure modes to sharpen decision boundary
        - Requires set_mined_negatives() on dataset

    Args:
        sources: List of DataSource objects. Defaults to DATA_SOURCES.
        phase2_path: Path to Phase 2 trained model.
        output_path: Where to save the trained model.
        subsample_pairs: Max pairs to sample per source.
        epochs: Number of training epochs.
        batch_size: Training batch size.
        lr: Learning rate.
        negative_stage: 'A' for ortho-phonetic negatives, 'B' for model-mined
    """
    # Handle defaults
    if sources is None:
        sources = DATA_SOURCES

    data_paths = [s.path for s in sources]
    oversample_factors = [s.oversample for s in sources]

    print("=" * 60)
    print("Phase 3: Generalization Training (Curriculum Hard Negatives)")
    print(f"  Negative Stage: {negative_stage}")
    print("  Architecture: BiLSTM + Self-Attention + Attention Pooling")
    print("=" * 60)

    # Log data sources
    print("\nData sources:")
    for source in sources:
        print(f"  {source.name}: {source.path} (oversample: {source.oversample}x)")
    print()

    # Load vocabularies
    vocab_dir = os.path.dirname(phase2_path) or '.'
    base_name = os.path.splitext(os.path.basename(phase2_path))[0]
    char_vocab = CharVocab.load(os.path.join(vocab_dir, f'{base_name}_char_vocab.pkl'))
    lang_vocab = LangVocab.load(os.path.join(vocab_dir, f'{base_name}_lang_vocab.pkl'))

    train_dataset = MultiSourcePhase3Dataset(
        data_paths, oversample_factors,
        char_vocab, lang_vocab,
        split='train', subsample_pairs=subsample_pairs,
        negative_stage=negative_stage
    )
    val_dataset = MultiSourcePhase3Dataset(
        data_paths, oversample_factors,
        char_vocab, lang_vocab,
        split='val', subsample_pairs=subsample_pairs,
        negative_stage=negative_stage
    )

    print(f"Training pairs: {len(train_dataset):,}", flush=True)
    print(f"Validation pairs: {len(val_dataset):,}", flush=True)

    # Use num_workers=0 to avoid HDF5 multiprocessing issues
    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        collate_fn=collate_phase3, num_workers=0, pin_memory=True
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        collate_fn=collate_phase3, num_workers=0, pin_memory=True
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Device: {device}", flush=True)

    # Load Phase 2 models
    checkpoint = torch.load(phase2_path, map_location=device)

    phonetic_encoder = PhoneticEncoder().to(device)
    phonetic_encoder.load_state_dict(checkpoint['phonetic_state'])

    char_encoder = CharEncoder(
        vocab_size=char_vocab.vocab_size,
        num_langs=lang_vocab.next_id
    ).to(device)
    char_encoder.load_state_dict(checkpoint['char_state'])

    # Create hybrid model
    model = HybridPhoneticModel(phonetic_encoder, char_encoder).to(device)

    # CRITICAL: Freeze phonetic encoder (preserve Teacher's phonetic grounding)
    for param in model.phonetic_encoder.parameters():
        param.requires_grad = False
    print("Phonetic encoder frozen")

    # CRITICAL: Freeze gate (learned in Phase 2, don't corrupt it)
    for param in model.gate.parameters():
        param.requires_grad = False
    print("Gate frozen")

    # Only train character encoder
    optimizer = torch.optim.Adam(
        model.char_encoder.parameters(),
        lr=lr
    )
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', patience=3, factor=0.5
    )
    criterion = TripletLoss()

    best_val_loss = float('inf')

    for epoch in range(epochs):
        # Training
        model.train()
        train_loss = 0

        for batch in train_loader:
            anchor_ids, anchor_langs, anchor_lens = batch['anchor']
            pos_ids, pos_langs, pos_lens = batch['positive']
            neg_ids, neg_langs, neg_lens = batch['negative']

            anchor_ids = anchor_ids.to(device)
            anchor_langs = anchor_langs.to(device)
            anchor_lens = anchor_lens.to(device)
            pos_ids = pos_ids.to(device)
            pos_langs = pos_langs.to(device)
            pos_lens = pos_lens.to(device)
            neg_ids = neg_ids.to(device)
            neg_langs = neg_langs.to(device)
            neg_lens = neg_lens.to(device)

            optimizer.zero_grad()

            # Use character-only encoding in Phase 3
            anchor_emb = model.encode_char_only(anchor_ids, anchor_langs, anchor_lens)
            pos_emb = model.encode_char_only(pos_ids, pos_langs, pos_lens)
            neg_emb = model.encode_char_only(neg_ids, neg_langs, neg_lens)

            loss = criterion(anchor_emb, pos_emb, neg_emb)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.char_encoder.parameters(), max_norm=1.0)
            optimizer.step()
            train_loss += loss.item()

        # Validation
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                anchor_ids, anchor_langs, anchor_lens = batch['anchor']
                pos_ids, pos_langs, pos_lens = batch['positive']
                neg_ids, neg_langs, neg_lens = batch['negative']

                anchor_emb = model.encode_char_only(
                    anchor_ids.to(device), anchor_langs.to(device), anchor_lens.to(device)
                )
                pos_emb = model.encode_char_only(
                    pos_ids.to(device), pos_langs.to(device), pos_lens.to(device)
                )
                neg_emb = model.encode_char_only(
                    neg_ids.to(device), neg_langs.to(device), neg_lens.to(device)
                )

                loss = criterion(anchor_emb, pos_emb, neg_emb)
                val_loss += loss.item()

        train_loss /= len(train_loader)
        val_loss /= len(val_loader)
        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | Train: {train_loss:.4f} | Val: {val_loss:.4f}", flush=True)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save({
                'model_state': model.state_dict(),
                'char_vocab_size': char_vocab.vocab_size,
                'num_langs': lang_vocab.next_id,
                'epoch': epoch,
                'val_loss': val_loss,
                'negative_stage': negative_stage
            }, output_path)

            # Copy vocabularies to final output location
            final_vocab_dir = os.path.dirname(output_path) or '.'
            final_base_name = os.path.splitext(os.path.basename(output_path))[0]
            char_vocab.save(os.path.join(final_vocab_dir, f'{final_base_name}_char_vocab.pkl'))
            lang_vocab.save(os.path.join(final_vocab_dir, f'{final_base_name}_lang_vocab.pkl'))

            print(f"  → Saved best model (val_loss: {val_loss:.4f})", flush=True)

    print(f"\nPhase 3 complete. Final model saved to {output_path}")
    return model


def mine_hard_negatives(
    model: HybridPhoneticModel,
    data_path: str,
    char_vocab: CharVocab,
    lang_vocab: LangVocab,
    similarity_threshold: float = Config.STAGE_B_SIMILARITY_THRESHOLD,
    max_negatives_per_anchor: int = 10,
    device: str = 'cuda'
) -> Dict[int, List[int]]:
    """
    Mine hard negatives from model's false positives for Stage B training.

    Identifies pairs where:
    - Model similarity > threshold
    - Items are from different clusters (known non-identical)

    CONSTRAINTS (as specified):
    - No human labeling
    - Only reuse existing dataset exclusions (cluster != cluster)
    - Conservative threshold (high similarity only)

    Args:
        model: Trained model from Phase 3 Stage A
        data_path: HDF5 training data path
        similarity_threshold: Conservative threshold (default 0.85)
        max_negatives_per_anchor: Limit negatives per anchor

    Returns:
        Dict mapping anchor_idx to list of hard negative indices
    """
    import h5py
    from collections import defaultdict

    print("=" * 60)
    print("Mining Hard Negatives (Stage B)")
    print(f"  Similarity threshold: {similarity_threshold}")
    print("=" * 60)

    model.eval()
    device = torch.device(device if torch.cuda.is_available() else 'cpu')
    model.to(device)

    mined_negatives = defaultdict(list)

    with h5py.File(data_path, 'r') as f:
        items = f['items']
        total_items = f.attrs['total_items']

        # Build cluster index
        cluster_to_items = defaultdict(list)
        item_clusters = {}

        for idx in range(total_items):
            cluster_id = int(items['cluster_id'][idx])
            cluster_to_items[cluster_id].append(idx)
            item_clusters[idx] = cluster_id

        # Sample items for efficiency
        sample_size = min(10000, total_items)
        sampled_indices = list(range(total_items))
        import random
        random.shuffle(sampled_indices)
        sampled_indices = sampled_indices[:sample_size]

        print(f"Computing embeddings for {sample_size} items...")

        # Compute embeddings in batches
        embeddings = {}
        batch_size = 256

        for batch_start in range(0, len(sampled_indices), batch_size):
            batch_indices = sampled_indices[batch_start:batch_start + batch_size]

            char_ids_list = []
            lang_ids_list = []

            for idx in batch_indices:
                romanized = items['romanized'][idx]
                lang = items['lang'][idx]

                char_ids = char_vocab.encode(romanized)
                lang_id = lang_vocab.encode(lang)

                char_ids_list.append(torch.tensor(char_ids, dtype=torch.long))
                lang_ids_list.append(lang_id)

            # Pad and batch
            max_len = max(len(ids) for ids in char_ids_list)
            char_ids_padded = torch.zeros(len(batch_indices), max_len, dtype=torch.long)
            char_lengths = torch.tensor([len(ids) for ids in char_ids_list])

            for i, ids in enumerate(char_ids_list):
                char_ids_padded[i, :len(ids)] = ids

            lang_ids_tensor = torch.tensor(lang_ids_list, dtype=torch.long)

            with torch.no_grad():
                embs = model.encode_char_only(
                    char_ids_padded.to(device),
                    lang_ids_tensor.to(device),
                    char_lengths
                )
                embs = embs.cpu().numpy()

            for i, idx in enumerate(batch_indices):
                embeddings[idx] = embs[i]

        print(f"Finding false positives (sim > {similarity_threshold})...")

        # Find false positives
        import numpy as np

        fp_count = 0
        for i, idx_a in enumerate(sampled_indices):
            if i % 1000 == 0:
                print(f"  Processed {i}/{len(sampled_indices)}", end='\r')

            emb_a = embeddings[idx_a]
            cluster_a = item_clusters[idx_a]

            for idx_b in sampled_indices[i+1:]:
                cluster_b = item_clusters[idx_b]

                # Only consider cross-cluster pairs (known non-identical)
                if cluster_a == cluster_b:
                    continue

                emb_b = embeddings[idx_b]

                # Compute cosine similarity
                sim = float(np.dot(emb_a, emb_b))

                if sim > similarity_threshold:
                    # This is a false positive - model thinks they're similar but they're not
                    if len(mined_negatives[idx_a]) < max_negatives_per_anchor:
                        mined_negatives[idx_a].append(idx_b)
                        fp_count += 1
                    if len(mined_negatives[idx_b]) < max_negatives_per_anchor:
                        mined_negatives[idx_b].append(idx_a)
                        fp_count += 1

        print(f"\nMined {fp_count} hard negative pairs")
        print(f"Anchors with negatives: {len(mined_negatives)}")

    return dict(mined_negatives)