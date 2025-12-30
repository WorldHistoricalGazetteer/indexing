#!/usr/bin/env python3
"""
Phonetic Similarity Model for Multilingual Toponym Matching

A Student-Teacher architecture that learns phonetic embeddings from toponyms.
- Teacher: Epitran + PanPhon → IPA features → BiLSTM + Self-Attention (phonetically grounded)
- Student: anyascii + Language ID → BiLSTM + Self-Attention (universal fallback)

v2 Architecture Upgrades:
- BiLSTM + Lightweight Self-Attention (1-2 heads)
- Attention-Aware Pooling (replaces mean/last-state pooling)
- Curriculum Hard Negatives for Phase 3:
  - Stage A: Orthographically close, phonetically distant
  - Stage B: Model-mined false positives (optional)

Uses HDF5 for memory-efficient training on large datasets.
Memory footprint stays constant (~100MB) regardless of dataset size.

Training proceeds in three phases:
1. Train Teacher on IPA features (triplet loss)
2. Align Student to Teacher (MSE + cosine loss)
3. Fine-tune Student with curriculum hard negatives (triplet loss)

Requirements:
    pip install torch epitran panphon anyascii elasticsearch h5py

Usage:
    # Phase 0: Extract data from Elasticsearch
    python -m phonetics --phase 0 --es-host localhost:9200 --index places --output data.h5

    # Phase 1: Train phonetic encoder (Teacher)
    python -m phonetics --phase 1 --data data.h5 --output phase1.pt

    # Phase 2: Alignment training
    python -m phonetics --phase 2 --data data.h5 --phase1-model phase1.pt --output phase2.pt

    # Phase 3: Generalization training (Stage A - default)
    python -m phonetics --phase 3 --data data.h5 --phase2-model phase2.pt --output final_model.pt

    # Phase 3: Stage B (optional second pass with model-mined negatives)
    python -m phonetics --phase 3 --data data.h5 --phase2-model phase2.pt --output final_model_b.pt --negative-stage B --stage-a-model final_model.pt

    # Inference
    python -m phonetics --infer --model final_model.pt --toponym1 "London" --lang1 "en" --toponym2 "Londres" --lang2 "fr"
"""

import argparse
import os

import torch

from .config import Config


def main():
    parser = argparse.ArgumentParser(
        description='Phonetic Similarity Model v2 (with Self-Attention)',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--enrich', action='store_true',
                        help="Hydrate 'toponyms' index with phonetics")

    parser.add_argument('--phase', type=int, choices=[0, 1, 2, 3])
    parser.add_argument('--infer', action='store_true')

    # Data extraction (Phase 0)
    parser.add_argument('--es-host', default='localhost:9200')
    parser.add_argument('--index', default='places')
    parser.add_argument('--max-docs', type=int, default=None)
    parser.add_argument('-n', '--namespaces', default=None,
                        help='Comma-separated namespace prefixes to extract (e.g., -n gn or -n gn,wd)')
    parser.add_argument('--workers', type=int, default=12,
                        help='Number of parallel workers for enrichment (default: 12)')

    # Training
    parser.add_argument('--data', default='training_data.h5',
                        help='Training data file (HDF5 for phases 1-3)')
    parser.add_argument('--output', default='model.pt',
                        help='Output file (HDF5 for phase 0, .pt for phases 1-3)')
    parser.add_argument('--phase1-model', default='phase1.pt')
    parser.add_argument('--phase2-model', default='phase2.pt')
    parser.add_argument('--epochs', type=int, default=None)
    parser.add_argument('--batch-size', type=int, default=Config.BATCH_SIZE)
    parser.add_argument('--subsample-pairs', type=int, default=Config.SUBSAMPLE_PAIRS)
    parser.add_argument('--lr', type=float, default=Config.LEARNING_RATE)

    # Phase 3 curriculum options
    parser.add_argument('--negative-stage', choices=['A', 'B'], default='A',
                        help='Curriculum stage: A=ortho-phon divergent, B=model-mined')
    parser.add_argument('--stage-a-model', default=None,
                        help='Stage A model for mining negatives (required for Stage B)')

    # Inference
    parser.add_argument('--model', default='final_model.pt')
    parser.add_argument('--toponym1')
    parser.add_argument('--lang1')
    parser.add_argument('--toponym2')
    parser.add_argument('--lang2')
    parser.add_argument('--gpu', action='store_true')

    # Deduplication
    parser.add_argument('--fast-extract', action='store_true',
                        help='Fast extraction without deduplication')
    parser.add_argument('--deduplicate', action='store_true',
                        help='Deduplicate existing HDF5 file')
    parser.add_argument('--input', help='Input file for deduplication')

    args = parser.parse_args()

    if args.enrich:
        from .extraction import ToponymEnricher
        enricher = ToponymEnricher(
            es_host=args.es_host,
            index='toponyms',
            num_workers=args.workers,
            batch_size=args.batch_size
        )
        enricher.run()
        return

    # Parse namespaces if provided
    namespaces = None
    if args.namespaces:
        namespaces = [ns.strip() for ns in args.namespaces.split(',')]

    if args.infer:
        from .inference import PhoneticSimilarityModel

        if not all([args.toponym1, args.lang1, args.toponym2, args.lang2]):
            parser.error("Inference requires --toponym1, --lang1, --toponym2, --lang2")

        device = 'cuda' if args.gpu and torch.cuda.is_available() else 'cpu'
        model = PhoneticSimilarityModel(args.model, device=device)

        sim = model.similarity(args.toponym1, args.lang1, args.toponym2, args.lang2)

        print(f"\n'{args.toponym1}' ({args.lang1}) vs '{args.toponym2}' ({args.lang2})")
        print(f"Similarity: {sim:.4f}")

        from anyascii import anyascii
        rom1 = anyascii(args.toponym1).lower()
        rom2 = anyascii(args.toponym2).lower()
        print(f"Romanized: '{rom1}' vs '{rom2}'")

    elif args.fast_extract:
        from .extraction import TrainingDataExtractor
        extractor = TrainingDataExtractor(args.es_host, args.index)
        extractor.extract_optimized(args.output, namespaces=namespaces, max_docs=args.max_docs)

    elif args.phase == 0:
        from .extraction import TrainingDataExtractor
        extractor = TrainingDataExtractor(args.es_host, args.index)
        extractor.extract_optimized(args.output, namespaces=namespaces, max_docs=args.max_docs)

    elif args.phase == 1:
        from .training import train_phase1
        epochs = args.epochs or Config.PHASE1_EPOCHS
        train_phase1(
            output_path=args.output,
            epochs=epochs,
            subsample_pairs=args.subsample_pairs,
            batch_size=args.batch_size,
            lr=args.lr
        )

    elif args.phase == 2:
        from .training import train_phase2
        epochs = args.epochs or Config.PHASE2_EPOCHS
        train_phase2(
            phase1_path=args.phase1_model,
            output_path=args.output,
            epochs=epochs,
            batch_size=args.batch_size,
            lr=args.lr
        )

    elif args.phase == 3:
        from .training import train_phase3
        from .vocab import CharVocab, LangVocab
        from .models import PhoneticEncoder, CharEncoder, HybridPhoneticModel

        epochs = args.epochs or Config.PHASE3_EPOCHS
        mined_negatives = None

        if args.negative_stage == 'B':
            # Stage B requires a Stage A model for mining negatives
            if not args.stage_a_model:
                parser.error("Stage B requires --stage-a-model from Stage A training")

            from .mining import mine_hard_negatives
            from .training import DATA_SOURCES_PHASE3_OPTIMIZED

            print("Loading Stage A model for negative mining...")

            # Load vocabularies
            vocab_dir = os.path.dirname(args.stage_a_model) or '.'
            base_name = os.path.splitext(os.path.basename(args.stage_a_model))[0]
            char_vocab = CharVocab.load(os.path.join(vocab_dir, f'{base_name}_char_vocab.pkl'))
            lang_vocab = LangVocab.load(os.path.join(vocab_dir, f'{base_name}_lang_vocab.pkl'))

            # Load model
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            checkpoint = torch.load(args.stage_a_model, map_location=device)
            phonetic_encoder = PhoneticEncoder()
            char_encoder = CharEncoder(
                vocab_size=checkpoint.get('char_vocab_size', char_vocab.vocab_size),
                num_langs=checkpoint.get('num_langs', lang_vocab.next_id)
            )
            model = HybridPhoneticModel(phonetic_encoder, char_encoder)
            model.load_state_dict(checkpoint['model_state'])

            # Use optimized file for mining if available (to match training indices)
            # Only use the first/primary source for mining
            mining_data_path = args.data
            if DATA_SOURCES_PHASE3_OPTIMIZED and os.path.exists(DATA_SOURCES_PHASE3_OPTIMIZED[0].path):
                mining_data_path = DATA_SOURCES_PHASE3_OPTIMIZED[0].path
                print(f"Using optimized file for mining: {mining_data_path}")

            # Mine hard negatives (hybrid strategy: random + targeted)
            mined_negatives = mine_hard_negatives(
                model=model,
                data_path=mining_data_path,
                char_vocab=char_vocab,
                lang_vocab=lang_vocab,
                device=device,
                similarity_threshold=0.4,      # Lower threshold to catch more
                max_negatives_per_anchor=20,   # More negatives per anchor
                random_sample_size=1000000,     # Random pairs for background noise
                targeted_sample_size=1000000,   # Targeted pairs for spelling confusion
            )

            print(f"Mined {sum(len(v) for v in mined_negatives.values()):,} hard negatives "
                  f"for {len(mined_negatives):,} anchors")

        train_phase3(
            phase2_path=args.phase2_model,
            output_path=args.output,
            subsample_pairs=args.subsample_pairs,
            epochs=epochs,
            batch_size=args.batch_size,
            lr=args.lr or 5e-4,
            negative_stage=args.negative_stage,
            mined_negatives=mined_negatives
        )

    else:
        parser.print_help()


if __name__ == '__main__':
    main()