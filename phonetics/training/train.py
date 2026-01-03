# training/train.py
"""
Training loops for phonetic embedding models.

Implements the three-phase training pipeline:
- Phase 1: Train Teacher on phonetic features (triplet loss)
- Phase 2: Align Student to Teacher (distillation + noise)
- Phase 3: Fine-tune Student with hard negatives (contrastive)

Usage:
    python -m phonetics.training.train \
        --data-dir /ix1/whcdh/models/phonetic/data/v2 \
        --output-dir /ix1/whcdh/models/phonetic/checkpoints \
        --phase 1 \
        --epochs 50

    python -m phonetics.training.train \
        --data-dir /ix1/whcdh/models/phonetic/data/v2 \
        --output-dir /ix1/whcdh/models/phonetic/checkpoints \
        --phase 2 \
        --teacher-checkpoint phase1_best.pt \
        --epochs 30

    python -m phonetics.training.train \
        --data-dir /ix1/whcdh/models/phonetic/data/v2 \
        --output-dir /ix1/whcdh/models/phonetic/checkpoints \
        --phase 3 \
        --student-checkpoint phase2_best.pt \
        --epochs 20
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.models.models import (
    PhoneticEncoder, UniversalEncoder,
    TripletMarginLossWithMining, ContrastiveDistillationLoss,
    create_teacher, create_student,
    load_checkpoint, save_checkpoint,
)
from phonetics.vocab.char_vocab import (
    CharacterVocabulary, ScriptVocabulary, LanguageVocabulary
)
from phonetics.training.data_loading import (
    create_phase1_dataloader,
    create_phase2_dataloader,
    create_phase3_dataloader,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# Training Configuration
# ============================================================================

DEFAULT_CONFIG = {
    # Model architecture
    'embed_dim': 128,
    'hidden_dim': 128,
    'num_layers': 2,
    'dropout': 0.2,
    'lang_dropout': 0.5,

    # Training
    'batch_size': 128,
    'learning_rate': 1e-3,
    'weight_decay': 1e-5,
    'warmup_epochs': 2,
    'noise_prob': 0.3,

    # Loss
    'triplet_margin': 0.3,
    'mse_weight': 1.0,
    'cosine_weight': 1.0,

    # Misc
    'num_workers': 4,
    'log_interval': 100,
    'eval_interval': 1,  # Epochs
    'save_interval': 5,  # Epochs
}


# ============================================================================
# Phase 1: Teacher Training
# ============================================================================

def train_phase1(
        data_dir: Path,
        output_dir: Path,
        config: Dict,
        epochs: int = 50,
        device: str = 'cuda',
        resume_from: Optional[str] = None,
):
    """
    Train the Teacher model on phonetic features.

    Uses triplet loss with random negatives to learn phonetic similarity.
    """
    logger.info("=" * 60)
    logger.info("Phase 1: Training Teacher (PhoneticEncoder)")
    logger.info("=" * 60)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create model
    teacher = create_teacher(
        embed_dim=config['embed_dim'],
        hidden_dim=config['hidden_dim'],
        num_layers=config['num_layers'],
        dropout=config['dropout'],
    ).to(device)

    logger.info(f"Teacher parameters: {sum(p.numel() for p in teacher.parameters()):,}")

    # Create optimizer
    optimizer = AdamW(
        teacher.parameters(),
        lr=config['learning_rate'],
        weight_decay=config['weight_decay'],
    )

    # Resume if specified
    start_epoch = 0
    best_loss = float('inf')
    if resume_from:
        meta = load_checkpoint(resume_from, teacher, optimizer, device)
        start_epoch = meta['epoch'] + 1
        best_loss = meta['best_loss']
        logger.info(f"Resumed from epoch {start_epoch}")

    # Create data loaders
    train_loader = create_phase1_dataloader(
        data_dir, split='train',
        batch_size=config['batch_size'],
        num_workers=config['num_workers'],
    )
    val_loader = create_phase1_dataloader(
        data_dir, split='val',
        batch_size=config['batch_size'],
        num_workers=config['num_workers'],
        shuffle=False,
    )

    # Create scheduler
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1e-6)

    # Loss function
    criterion = TripletMarginLossWithMining(margin=config['triplet_margin'])

    # Training loop
    for epoch in range(start_epoch, epochs):
        teacher.train()
        epoch_loss = 0.0
        num_batches = 0

        iterator = train_loader
        if tqdm:
            iterator = tqdm(iterator, desc=f"Epoch {epoch + 1}/{epochs}")

        for batch_idx, batch in enumerate(iterator):
            # Move to device
            anchor_feats = batch['anchor_features'].to(device)
            anchor_lens = batch['anchor_lengths']
            pos_feats = batch['positive_features'].to(device)
            pos_lens = batch['positive_lengths']
            neg_feats = batch['negative_features'].to(device)
            neg_lens = batch['negative_lengths']

            # Forward pass
            anchor_emb = teacher(anchor_feats, anchor_lens)
            pos_emb = teacher(pos_feats, pos_lens)
            neg_emb = teacher(neg_feats, neg_lens)

            # Compute loss
            loss = criterion(anchor_emb, pos_emb, neg_emb)

            # Backward pass
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(teacher.parameters(), max_norm=1.0)
            optimizer.step()

            epoch_loss += loss.item()
            num_batches += 1

            if tqdm and batch_idx % config['log_interval'] == 0:
                iterator.set_postfix(loss=loss.item())

        avg_train_loss = epoch_loss / num_batches
        scheduler.step()

        # Validation
        if (epoch + 1) % config['eval_interval'] == 0:
            val_loss = evaluate_phase1(teacher, val_loader, criterion, device)

            logger.info(f"Epoch {epoch + 1}: train_loss={avg_train_loss:.4f}, val_loss={val_loss:.4f}")

            # Save best model
            if val_loss < best_loss:
                best_loss = val_loss
                save_checkpoint(
                    output_dir / 'phase1_best.pt',
                    teacher, optimizer, epoch, 0, best_loss, config
                )
                logger.info(f"  Saved best model (val_loss={val_loss:.4f})")

        # Regular checkpoint
        if (epoch + 1) % config['save_interval'] == 0:
            save_checkpoint(
                output_dir / f'phase1_epoch{epoch + 1}.pt',
                teacher, optimizer, epoch, 0, best_loss, config
            )

    logger.info(f"Phase 1 complete. Best val_loss: {best_loss:.4f}")
    return teacher


def evaluate_phase1(
        model: PhoneticEncoder,
        dataloader,
        criterion,
        device: str,
) -> float:
    """Evaluate Teacher model on validation set."""
    model.eval()
    total_loss = 0.0
    num_batches = 0

    with torch.no_grad():
        for batch in dataloader:
            anchor_feats = batch['anchor_features'].to(device)
            anchor_lens = batch['anchor_lengths']
            pos_feats = batch['positive_features'].to(device)
            pos_lens = batch['positive_lengths']
            neg_feats = batch['negative_features'].to(device)
            neg_lens = batch['negative_lengths']

            anchor_emb = model(anchor_feats, anchor_lens)
            pos_emb = model(pos_feats, pos_lens)
            neg_emb = model(neg_feats, neg_lens)

            loss = criterion(anchor_emb, pos_emb, neg_emb)
            total_loss += loss.item()
            num_batches += 1

    return total_loss / num_batches


# ============================================================================
# Phase 2: Student-Teacher Alignment
# ============================================================================

def train_phase2(
        data_dir: Path,
        output_dir: Path,
        config: Dict,
        teacher_checkpoint: str,
        epochs: int = 30,
        device: str = 'cuda',
        resume_from: Optional[str] = None,
):
    """
    Train the Student model to align with Teacher.

    Uses distillation loss (MSE + cosine) with noise augmentation
    to learn robust phonetic representations.
    """
    logger.info("=" * 60)
    logger.info("Phase 2: Training Student (UniversalEncoder)")
    logger.info("=" * 60)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load vocabularies
    vocab_dir = Path(data_dir) / 'vocab'
    char_vocab = CharacterVocabulary.load(vocab_dir / 'char_vocab.json')
    script_vocab = ScriptVocabulary.load(vocab_dir / 'script_vocab.json')
    lang_vocab = LanguageVocabulary.load(vocab_dir / 'lang_vocab.json')

    logger.info(f"Vocabularies: chars={len(char_vocab)}, scripts={len(script_vocab)}, langs={len(lang_vocab)}")

    # Load Teacher (frozen)
    teacher = create_teacher(
        embed_dim=config['embed_dim'],
        hidden_dim=config['hidden_dim'],
    ).to(device)
    load_checkpoint(teacher_checkpoint, teacher, device=device)
    teacher.eval()
    for param in teacher.parameters():
        param.requires_grad = False

    logger.info("Loaded and frozen Teacher model")

    # Create Student
    student = create_student(
        vocab_size=len(char_vocab),
        num_scripts=len(script_vocab),
        num_langs=len(lang_vocab),
        embed_dim=config['embed_dim'],
        hidden_dim=config['hidden_dim'],
        num_layers=config['num_layers'],
        dropout=config['dropout'],
        lang_dropout=config['lang_dropout'],
    ).to(device)

    logger.info(f"Student parameters: {sum(p.numel() for p in student.parameters()):,}")

    # Create optimizer
    optimizer = AdamW(
        student.parameters(),
        lr=config['learning_rate'],
        weight_decay=config['weight_decay'],
    )

    # Resume if specified
    start_epoch = 0
    best_loss = float('inf')
    if resume_from:
        meta = load_checkpoint(resume_from, student, optimizer, device)
        start_epoch = meta['epoch'] + 1
        best_loss = meta['best_loss']
        logger.info(f"Resumed from epoch {start_epoch}")

    # Create data loaders
    train_loader = create_phase2_dataloader(
        data_dir, char_vocab, script_vocab, lang_vocab,
        split='train',
        batch_size=config['batch_size'],
        num_workers=config['num_workers'],
        noise_prob=config['noise_prob'],
    )
    val_loader = create_phase2_dataloader(
        data_dir, char_vocab, script_vocab, lang_vocab,
        split='val',
        batch_size=config['batch_size'],
        num_workers=config['num_workers'],
        noise_prob=0.0,  # No noise for validation
    )

    # Scheduler - use CosineAnnealingLR for stable alignment
    # (OneCycleLR is too aggressive for distillation)
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1e-6)

    # Loss function
    criterion = ContrastiveDistillationLoss(
        mse_weight=config['mse_weight'],
        cosine_weight=config['cosine_weight'],
    )

    # Training loop
    for epoch in range(start_epoch, epochs):
        student.train()
        epoch_loss = 0.0
        epoch_metrics = {'mse_loss': 0.0, 'cosine_loss': 0.0, 'cosine_sim': 0.0}
        num_batches = 0

        iterator = train_loader
        if tqdm:
            iterator = tqdm(iterator, desc=f"Epoch {epoch + 1}/{epochs}")

        for batch_idx, batch in enumerate(iterator):
            # Move to device
            char_ids = batch['char_ids'].to(device)
            char_lengths = batch['char_lengths']
            script_ids = batch['script_ids'].to(device)
            lang_ids = batch['lang_ids'].to(device)
            features = batch['features'].to(device)
            feature_lengths = batch['feature_lengths']

            # Get Teacher embeddings (frozen)
            with torch.no_grad():
                teacher_emb = teacher(features, feature_lengths)

            # Get Student embeddings
            student_emb = student(char_ids, script_ids, lang_ids, char_lengths)

            # Compute loss
            loss, metrics = criterion(student_emb, teacher_emb)

            # Backward pass
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(student.parameters(), max_norm=1.0)
            optimizer.step()

            epoch_loss += loss.item()
            for k, v in metrics.items():
                epoch_metrics[k] += v
            num_batches += 1

            if tqdm and batch_idx % config['log_interval'] == 0:
                iterator.set_postfix(loss=loss.item(), sim=metrics['cosine_sim'])

        avg_train_loss = epoch_loss / num_batches
        avg_metrics = {k: v / num_batches for k, v in epoch_metrics.items()}
        scheduler.step()

        # Validation
        if (epoch + 1) % config['eval_interval'] == 0:
            val_loss, val_metrics = evaluate_phase2(
                student, teacher, val_loader, criterion, device
            )

            logger.info(
                f"Epoch {epoch + 1}: train_loss={avg_train_loss:.4f}, "
                f"val_loss={val_loss:.4f}, val_sim={val_metrics['cosine_sim']:.4f}"
            )

            # Save best model
            if val_loss < best_loss:
                best_loss = val_loss
                save_checkpoint(
                    output_dir / 'phase2_best.pt',
                    student, optimizer, epoch, 0, best_loss, config
                )
                logger.info(f"  Saved best model")

        # Regular checkpoint
        if (epoch + 1) % config['save_interval'] == 0:
            save_checkpoint(
                output_dir / f'phase2_epoch{epoch + 1}.pt',
                student, optimizer, epoch, 0, best_loss, config
            )

    logger.info(f"Phase 2 complete. Best val_loss: {best_loss:.4f}")
    return student


def evaluate_phase2(
        student: UniversalEncoder,
        teacher: PhoneticEncoder,
        dataloader,
        criterion,
        device: str,
) -> tuple:
    """Evaluate Student-Teacher alignment on validation set."""
    student.eval()
    total_loss = 0.0
    total_metrics = {'mse_loss': 0.0, 'cosine_loss': 0.0, 'cosine_sim': 0.0}
    num_batches = 0

    with torch.no_grad():
        for batch in dataloader:
            char_ids = batch['char_ids'].to(device)
            char_lengths = batch['char_lengths']
            script_ids = batch['script_ids'].to(device)
            lang_ids = batch['lang_ids'].to(device)
            features = batch['features'].to(device)
            feature_lengths = batch['feature_lengths']

            teacher_emb = teacher(features, feature_lengths)
            student_emb = student(char_ids, script_ids, lang_ids, char_lengths)

            loss, metrics = criterion(student_emb, teacher_emb)

            total_loss += loss.item()
            for k, v in metrics.items():
                total_metrics[k] += v
            num_batches += 1

    avg_loss = total_loss / num_batches
    avg_metrics = {k: v / num_batches for k, v in total_metrics.items()}

    return avg_loss, avg_metrics


# ============================================================================
# Phase 3: Contrastive Fine-tuning
# ============================================================================

def train_phase3(
        data_dir: Path,
        output_dir: Path,
        config: Dict,
        student_checkpoint: str,
        epochs: int = 20,
        device: str = 'cuda',
        resume_from: Optional[str] = None,
):
    """
    Fine-tune Student with contrastive loss and hard negatives.

    Uses triplet loss with orthographically similar negatives
    to improve discrimination of similar-looking names.
    """
    logger.info("=" * 60)
    logger.info("Phase 3: Fine-tuning Student (Hard Negatives)")
    logger.info("=" * 60)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load vocabularies
    vocab_dir = Path(data_dir) / 'vocab'
    char_vocab = CharacterVocabulary.load(vocab_dir / 'char_vocab.json')
    script_vocab = ScriptVocabulary.load(vocab_dir / 'script_vocab.json')
    lang_vocab = LanguageVocabulary.load(vocab_dir / 'lang_vocab.json')

    # Load Student
    student = create_student(
        vocab_size=len(char_vocab),
        num_scripts=len(script_vocab),
        num_langs=len(lang_vocab),
        embed_dim=config['embed_dim'],
        hidden_dim=config['hidden_dim'],
        num_layers=config['num_layers'],
        dropout=config['dropout'],
        lang_dropout=config['lang_dropout'],
    ).to(device)

    load_checkpoint(student_checkpoint, student, device=device)
    logger.info("Loaded Student from Phase 2")

    # Create optimizer (lower learning rate for fine-tuning)
    optimizer = AdamW(
        student.parameters(),
        lr=config['learning_rate'] * 0.1,
        weight_decay=config['weight_decay'],
    )

    # Resume if specified
    start_epoch = 0
    best_loss = float('inf')
    if resume_from:
        meta = load_checkpoint(resume_from, student, optimizer, device)
        start_epoch = meta['epoch'] + 1
        best_loss = meta['best_loss']

    # Create data loaders
    train_loader = create_phase3_dataloader(
        data_dir, char_vocab, script_vocab, lang_vocab,
        split='train',
        batch_size=config['batch_size'],
        num_workers=config['num_workers'],
        noise_prob=config['noise_prob'],
    )
    val_loader = create_phase3_dataloader(
        data_dir, char_vocab, script_vocab, lang_vocab,
        split='val',
        batch_size=config['batch_size'],
        num_workers=config['num_workers'],
        noise_prob=0.0,
    )

    # Scheduler
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1e-7)

    # Loss function
    criterion = TripletMarginLossWithMining(margin=config['triplet_margin'])

    # Training loop
    for epoch in range(start_epoch, epochs):
        student.train()
        epoch_loss = 0.0
        num_batches = 0

        iterator = train_loader
        if tqdm:
            iterator = tqdm(iterator, desc=f"Epoch {epoch + 1}/{epochs}")

        for batch_idx, batch in enumerate(iterator):
            # Move to device
            anchor_chars = batch['anchor_char_ids'].to(device)
            anchor_lens = batch['anchor_char_lengths']
            anchor_scripts = batch['anchor_script_ids'].to(device)
            anchor_langs = batch['anchor_lang_ids'].to(device)

            pos_chars = batch['positive_char_ids'].to(device)
            pos_lens = batch['positive_char_lengths']
            pos_scripts = batch['positive_script_ids'].to(device)
            pos_langs = batch['positive_lang_ids'].to(device)

            neg_chars = batch['negative_char_ids'].to(device)
            neg_lens = batch['negative_char_lengths']
            neg_scripts = batch['negative_script_ids'].to(device)
            neg_langs = batch['negative_lang_ids'].to(device)

            # Forward pass
            anchor_emb = student(anchor_chars, anchor_scripts, anchor_langs, anchor_lens)
            pos_emb = student(pos_chars, pos_scripts, pos_langs, pos_lens)
            neg_emb = student(neg_chars, neg_scripts, neg_langs, neg_lens)

            # Compute loss
            loss = criterion(anchor_emb, pos_emb, neg_emb)

            # Backward pass
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(student.parameters(), max_norm=1.0)
            optimizer.step()

            epoch_loss += loss.item()
            num_batches += 1

            if tqdm and batch_idx % config['log_interval'] == 0:
                iterator.set_postfix(loss=loss.item())

        avg_train_loss = epoch_loss / num_batches
        scheduler.step()

        # Validation
        if (epoch + 1) % config['eval_interval'] == 0:
            val_loss = evaluate_phase3(student, val_loader, criterion, device)

            logger.info(f"Epoch {epoch + 1}: train_loss={avg_train_loss:.4f}, val_loss={val_loss:.4f}")

            # Save best model
            if val_loss < best_loss:
                best_loss = val_loss
                save_checkpoint(
                    output_dir / 'phase3_best.pt',
                    student, optimizer, epoch, 0, best_loss, config
                )
                logger.info(f"  Saved best model")

        # Regular checkpoint
        if (epoch + 1) % config['save_interval'] == 0:
            save_checkpoint(
                output_dir / f'phase3_epoch{epoch + 1}.pt',
                student, optimizer, epoch, 0, best_loss, config
            )

    # Save final model
    save_checkpoint(
        output_dir / 'final_model.pt',
        student, optimizer, epochs - 1, 0, best_loss, config
    )

    logger.info(f"Phase 3 complete. Best val_loss: {best_loss:.4f}")
    return student


def evaluate_phase3(
        model: UniversalEncoder,
        dataloader,
        criterion,
        device: str,
) -> float:
    """Evaluate Student on contrastive validation set."""
    model.eval()
    total_loss = 0.0
    num_batches = 0

    with torch.no_grad():
        for batch in dataloader:
            anchor_chars = batch['anchor_char_ids'].to(device)
            anchor_lens = batch['anchor_char_lengths']
            anchor_scripts = batch['anchor_script_ids'].to(device)
            anchor_langs = batch['anchor_lang_ids'].to(device)

            pos_chars = batch['positive_char_ids'].to(device)
            pos_lens = batch['positive_char_lengths']
            pos_scripts = batch['positive_script_ids'].to(device)
            pos_langs = batch['positive_lang_ids'].to(device)

            neg_chars = batch['negative_char_ids'].to(device)
            neg_lens = batch['negative_char_lengths']
            neg_scripts = batch['negative_script_ids'].to(device)
            neg_langs = batch['negative_lang_ids'].to(device)

            anchor_emb = model(anchor_chars, anchor_scripts, anchor_langs, anchor_lens)
            pos_emb = model(pos_chars, pos_scripts, pos_langs, pos_lens)
            neg_emb = model(neg_chars, neg_scripts, neg_langs, neg_lens)

            loss = criterion(anchor_emb, pos_emb, neg_emb)
            total_loss += loss.item()
            num_batches += 1

    return total_loss / num_batches


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Train phonetic embedding models')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--phase', type=int, required=True, choices=[1, 2, 3])
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--learning-rate', type=float, default=1e-3)
    parser.add_argument('--device', type=str, default='cuda' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--teacher-checkpoint', type=str, help='Teacher checkpoint for Phase 2')
    parser.add_argument('--student-checkpoint', type=str, help='Student checkpoint for Phase 3')
    parser.add_argument('--resume-from', type=str, help='Resume from checkpoint')
    parser.add_argument('--config', type=str, help='JSON config file')

    args = parser.parse_args()

    # Load config
    config = DEFAULT_CONFIG.copy()
    if args.config:
        with open(args.config) as f:
            config.update(json.load(f))

    # Override with command line args
    config['batch_size'] = args.batch_size
    config['learning_rate'] = args.learning_rate

    logger.info(f"Config: {json.dumps(config, indent=2)}")
    logger.info(f"Device: {args.device}")

    # Run appropriate phase
    if args.phase == 1:
        train_phase1(
            args.data_dir, args.output_dir, config,
            epochs=args.epochs, device=args.device,
            resume_from=args.resume_from,
        )

    elif args.phase == 2:
        if not args.teacher_checkpoint:
            logger.error("Phase 2 requires --teacher-checkpoint")
            sys.exit(1)
        train_phase2(
            args.data_dir, args.output_dir, config,
            teacher_checkpoint=args.teacher_checkpoint,
            epochs=args.epochs, device=args.device,
            resume_from=args.resume_from,
        )

    elif args.phase == 3:
        if not args.student_checkpoint:
            logger.error("Phase 3 requires --student-checkpoint")
            sys.exit(1)
        train_phase3(
            args.data_dir, args.output_dir, config,
            student_checkpoint=args.student_checkpoint,
            epochs=args.epochs, device=args.device,
            resume_from=args.resume_from,
        )


if __name__ == '__main__':
    main()