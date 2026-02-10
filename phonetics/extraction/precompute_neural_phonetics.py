"""
Precompute IPA and PanPhon features for neural G2P backends (GPU).

This script handles languages that require neural models for G2P conversion:
- CharsiuG2P: Mandarin (zh), Korean (ko), Cantonese (yue), Gan (gan), Wu (wuu)
- Phonikud: Hebrew (he)

These models are impractical on CPU (5+ hours for ~2000 items on a 64-core HTC node).
On GPU they process the same batch in minutes with batched inference.

Output: Parquet file mapping toponym_id -> (ipa, panphon_features, panphon_embedding).
This is consumed by rebuild_toponyms_index.py via --precomputed-phonetics.

Usage:
    # On GPU node (A100):
    python -m phonetics.extraction.precompute_neural_phonetics \\
        --db-path /ix1/ishi/data/toponyms.db \\
        --output /ix1/ishi/models/phonetic/data/v6/neural_phonetics.parquet \\
        --training-namespaces gn wd tgn \\
        --batch-size 64 \\
        --device cuda

    # Then pass to rebuild:
    python -m phonetics.extraction.rebuild_toponyms_index \\
        --resume --confirm \\
        --precomputed-phonetics /ix1/ishi/models/phonetic/data/v6/neural_phonetics.parquet
"""

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module='pkg_resources')
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
warnings.filterwarnings("ignore", message=".*tokenizer class.*")
warnings.filterwarnings("ignore", category=FutureWarning)

import argparse
import json
import logging
import struct
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from phonetics.utils.script_detection import Script
from processing.settings import IX1_BASE

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Languages routed to each neural backend
CHARSIU_LANGS = {'zh', 'ko', 'gan', 'wuu', 'yue'}
PHONIKUD_LANGS = {'he'}
NEURAL_LANGS = CHARSIU_LANGS | PHONIKUD_LANGS

# CharsiuG2P language code mapping
CHARSIU_LANG_MAP = {
    'zh': 'cmn',
    'ko': 'kor',
    'gan': 'cmn',
    'wuu': 'cmn',
    'yue': 'yue',
}


class BatchCharsiuG2P:
    """
    CharsiuG2P wrapper with batched inference for GPU efficiency.

    Single-item inference: ~0.5-2s per item on GPU, ~5-15s on CPU.
    Batched inference (64 items): ~2-5s total on GPU = 30-80x speedup.
    """

    def __init__(self, device='cuda'):
        import torch
        import transformers

        logger.info("Loading CharsiuG2P model...")
        self.model = transformers.T5ForConditionalGeneration.from_pretrained(
            "charsiu/g2p_multilingual_byT5_small_100"
        )
        self.tokenizer = transformers.ByT5Tokenizer.from_pretrained("google/byt5-small")
        self.device = device
        self.model.to(device)
        self.model.eval()
        self.torch = torch
        logger.info(f"CharsiuG2P loaded on {device}")

    def transliterate_batch(self, items: List[Tuple[str, str]]) -> List[Optional[str]]:
        """
        Batch G2P conversion.

        Args:
            items: List of (text, lang) tuples

        Returns:
            List of IPA strings (None for failures), same order as input
        """
        if not items:
            return []

        input_texts = []
        for text, lang in items:
            char_iso = CHARSIU_LANG_MAP.get(lang, lang)
            input_texts.append(f"<{char_iso}>: {text}")

        try:
            inputs = self.tokenizer(
                input_texts,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=256,
            ).to(self.device)

            with self.torch.no_grad():
                outputs = self.model.generate(
                    **inputs,
                    max_new_tokens=256,
                )

            results = []
            for output in outputs:
                decoded = self.tokenizer.decode(output, skip_special_tokens=True)
                results.append(decoded if decoded.strip() else None)

            return results

        except Exception as e:
            logger.warning(f"Batch CharsiuG2P failed ({len(items)} items): {e}")
            # Fall back to individual processing
            results = []
            for text, lang in items:
                try:
                    result = self._single_transliterate(text, lang)
                    results.append(result)
                except Exception:
                    results.append(None)
            return results

    def _single_transliterate(self, text: str, lang: str) -> Optional[str]:
        """Single-item fallback."""
        char_iso = CHARSIU_LANG_MAP.get(lang, lang)
        input_text = f"<{char_iso}>: {text}"
        inputs = self.tokenizer(input_text, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            outputs = self.model.generate(**inputs, max_new_tokens=256)
        decoded = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
        return decoded if decoded.strip() else None


class BatchPhonikud:
    """
    Phonikud wrapper. Phonikud processes items individually (transformer-based
    diacritization), but we wrap it consistently for the pipeline.
    """

    def __init__(self):
        logger.info("Loading Phonikud model...")
        import phonikud as phonikud_module
        self._mod = phonikud_module
        logger.info("Phonikud loaded")

    def transliterate_batch(self, items: List[Tuple[str, str]]) -> List[Optional[str]]:
        """Process items individually (Phonikud doesn't support native batching)."""
        results = []
        for text, lang in items:
            try:
                ipa = self._mod.phonemize(text)
                results.append(ipa if ipa and ipa.strip() else None)
            except Exception:
                results.append(None)
        return results


class PanPhonConverter:
    """PanPhon feature extraction and embedding computation."""

    def __init__(self):
        import panphon
        self._ft = panphon.FeatureTable()

    def to_features_and_embedding(self, ipa: str) -> Tuple[Optional[bytes], Optional[List[float]]]:
        """
        Compute both raw features (packed bytes) and 192-dim embedding.

        Returns:
            (packed_features, embedding_192) tuple
        """
        if not ipa:
            return None, None

        try:
            segments = self._ft.word_fts(ipa)
            if not segments:
                return None, None

            # Raw features (for DuckDB storage / training)
            features = []
            for seg in segments:
                features.extend(seg.numeric())
            packed = struct.pack(f'{len(features)}f', *features)

            # 192-dim positional embedding (for ES)
            num_segments = len(segments)
            num_bins = 8
            features_per_bin = 24
            bins = [[0.0] * features_per_bin for _ in range(num_bins)]
            bin_counts = [0] * num_bins

            for seg_idx, seg in enumerate(segments):
                position = seg_idx / num_segments
                bin_idx = min(int(position * num_bins), num_bins - 1)
                feats = seg.numeric()
                for i, val in enumerate(feats):
                    bins[bin_idx][i] += val
                bin_counts[bin_idx] += 1

            embedding = []
            for bin_idx in range(num_bins):
                if bin_counts[bin_idx] > 0:
                    embedding.extend(v / bin_counts[bin_idx] for v in bins[bin_idx])
                else:
                    embedding.extend([0.0] * features_per_bin)

            return packed, embedding

        except Exception:
            return None, None


def query_neural_toponyms(db_path: str, training_namespaces: List[str]) -> List[Tuple]:
    """
    Query DuckDB for toponyms that need neural G2P backends.

    Returns:
        List of (toponym_id, name, lang, script) tuples
    """
    conn = duckdb.connect(db_path, read_only=True)

    neural_langs_sql = ','.join(f"'{l}'" for l in NEURAL_LANGS)
    ns_sql = ','.join(f"'{ns}'" for ns in training_namespaces)

    query = f'''
        SELECT DISTINCT t.toponym_id, t.name, t.lang, t.script
        FROM toponyms t
        JOIN toponym_namespaces tn ON t.toponym_id = tn.toponym_id
        WHERE t.lang IN ({neural_langs_sql})
          AND tn.namespace IN ({ns_sql})
          AND t.name IS NOT NULL
          AND t.name != ''
    '''

    rows = conn.execute(query).fetchall()
    conn.close()

    return rows


def main():
    parser = argparse.ArgumentParser(
        description='Precompute neural G2P phonetics on GPU (CharsiuG2P + Phonikud)'
    )
    parser.add_argument('--db-path', type=Path,
                        default=f'{IX1_BASE}/data/toponyms.db',
                        help='DuckDB database path')
    parser.add_argument('--output', type=Path, required=True,
                        help='Output Parquet file path')
    parser.add_argument('--training-namespaces', nargs='+', default=['gn', 'wd', 'tgn'],
                        help='Namespaces to process')
    parser.add_argument('--batch-size', type=int, default=64,
                        help='Batch size for GPU inference (default: 64)')
    parser.add_argument('--device', default='cuda',
                        help='Device for neural models (cuda/cpu)')
    parser.add_argument('--scratch-dir', type=Path, default=None,
                        help='Scratch directory for HuggingFace cache')

    args = parser.parse_args()

    # HuggingFace cache
    if args.scratch_dir:
        import os
        os.environ['HF_HOME'] = str(args.scratch_dir / 'hf_cache')
        os.environ['TRANSFORMERS_CACHE'] = str(args.scratch_dir / 'hf_cache')

    args.output.parent.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Step 1: Query DuckDB for neural-language toponyms
    # -------------------------------------------------------------------------
    logger.info(f"Querying DuckDB: {args.db_path}")
    logger.info(f"Neural languages: {sorted(NEURAL_LANGS)}")
    logger.info(f"Training namespaces: {args.training_namespaces}")

    rows = query_neural_toponyms(str(args.db_path), args.training_namespaces)
    logger.info(f"Found {len(rows):,} toponyms needing neural G2P")

    if not rows:
        logger.warning("No neural-language toponyms found. Nothing to do.")
        # Write empty Parquet so downstream doesn't fail
        empty = pa.table({
            'toponym_id': pa.array([], type=pa.string()),
            'ipa': pa.array([], type=pa.string()),
            'panphon_features': pa.array([], type=pa.binary()),
            'panphon_embedding': pa.array([], type=pa.list_(pa.float32())),
        })
        pq.write_table(empty, args.output)
        logger.info(f"Empty Parquet written to {args.output}")
        return

    # Split by backend
    charsiu_items = [(tid, name, lang, script) for tid, name, lang, script in rows
                     if lang in CHARSIU_LANGS]
    phonikud_items = [(tid, name, lang, script) for tid, name, lang, script in rows
                      if lang in PHONIKUD_LANGS]

    logger.info(f"  CharsiuG2P items: {len(charsiu_items):,} (zh/ko/gan/wuu/yue)")
    logger.info(f"  Phonikud items:   {len(phonikud_items):,} (he)")

    # -------------------------------------------------------------------------
    # Step 2: Initialize models
    # -------------------------------------------------------------------------
    panphon = PanPhonConverter()

    charsiu = None
    if charsiu_items:
        charsiu = BatchCharsiuG2P(device=args.device)

    phonikud = None
    if phonikud_items:
        phonikud = BatchPhonikud()

    # -------------------------------------------------------------------------
    # Step 3: Process in batches
    # -------------------------------------------------------------------------
    results = {
        'toponym_id': [],
        'ipa': [],
        'panphon_features': [],
        'panphon_embedding': [],
    }

    total_processed = 0
    total_with_ipa = 0
    total_with_embedding = 0
    batch_size = args.batch_size

    def process_backend(items, model, backend_name):
        nonlocal total_processed, total_with_ipa, total_with_embedding

        logger.info(f"Processing {len(items):,} items with {backend_name}...")
        start_time = time.time()

        for batch_start in range(0, len(items), batch_size):
            batch = items[batch_start:batch_start + batch_size]
            texts_langs = [(name, lang) for _, name, lang, _ in batch]
            ids = [tid for tid, _, _, _ in batch]

            ipa_results = model.transliterate_batch(texts_langs)

            for tid, ipa in zip(ids, ipa_results):
                if not ipa:
                    continue

                packed, embedding = panphon.to_features_and_embedding(ipa)
                if not embedding:
                    continue

                results['toponym_id'].append(tid)
                results['ipa'].append(ipa)
                results['panphon_features'].append(packed)
                results['panphon_embedding'].append(embedding)

                total_with_ipa += 1
                total_with_embedding += 1

            total_processed += len(batch)

            if (batch_start + batch_size) % (batch_size * 50) == 0 or batch_start + batch_size >= len(items):
                elapsed = time.time() - start_time
                rate = total_processed / elapsed if elapsed > 0 else 0
                logger.info(
                    f"  {backend_name}: {batch_start + len(batch):,}/{len(items):,} "
                    f"({rate:.0f} items/sec, "
                    f"{total_with_ipa:,} IPA, {total_with_embedding:,} embeddings)"
                )

        elapsed = time.time() - start_time
        logger.info(f"  {backend_name} complete in {elapsed:.1f}s")

    if charsiu and charsiu_items:
        process_backend(charsiu_items, charsiu, 'CharsiuG2P')

    if phonikud and phonikud_items:
        process_backend(phonikud_items, phonikud, 'Phonikud')

    # -------------------------------------------------------------------------
    # Step 4: Write Parquet
    # -------------------------------------------------------------------------
    logger.info(f"Writing {len(results['toponym_id']):,} results to {args.output}")

    table = pa.table({
        'toponym_id': pa.array(results['toponym_id'], type=pa.string()),
        'ipa': pa.array(results['ipa'], type=pa.string()),
        'panphon_features': pa.array(results['panphon_features'], type=pa.binary()),
        'panphon_embedding': pa.array(results['panphon_embedding'],
                                       type=pa.list_(pa.float32())),
    })

    pq.write_table(table, args.output, compression='snappy')

    logger.info("=" * 60)
    logger.info("PRECOMPUTE COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Total queried:     {len(rows):,}")
    logger.info(f"  Total processed:   {total_processed:,}")
    logger.info(f"  With IPA:          {total_with_ipa:,}")
    logger.info(f"  With embedding:    {total_with_embedding:,}")
    logger.info(f"  Output:            {args.output}")
    logger.info(f"  Output size:       {args.output.stat().st_size / 1024 / 1024:.1f} MB")
    logger.info("")
    logger.info("Next: pass to rebuild_toponyms_index.py via --precomputed-phonetics")


if __name__ == '__main__':
    main()