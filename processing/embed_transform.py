#!/usr/bin/env python3
"""
Phase 2: Compute embeddings from Parquet files using GPU.
Run on GPU node - maximizes GPU utilization with local scratch I/O.
"""

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd
import torch
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from processing.utilities import load_phonetic_model

GPU_BATCH_SIZE = 512


def parse_args():
    p = argparse.ArgumentParser(description="Compute embeddings from Parquet")
    p.add_argument("--input-dir", type=str, default="data/embed_pipeline",
                   help="Directory with raw Parquet chunks (on shared storage)")
    p.add_argument("--output-dir", type=str, default="data/embed_pipeline",
                   help="Output directory for vector Parquet files (on shared storage)")
    p.add_argument("--scratch-dir", type=str, default=None,
                   help="Local scratch directory (e.g., /scratch/slurm-$SLURM_JOB_ID)")
    p.add_argument("--model-version", type=int, required=True,
                   help="Model version tag for output")
    p.add_argument("--checkpoint", type=str, default=None,
                   help="Path to .pt checkpoint file")
    return p.parse_args()


def collate_batch(names, langs, model, device):
    """Tokenize and pad batch for GPU inference."""
    char_seqs = [model.char_vocab.encode(n) for n in names]
    lang_ids_list = [model.lang_vocab.encode(lang) for lang in langs]

    seq_lengths = [len(s) for s in char_seqs]
    max_len = max(seq_lengths) if seq_lengths else 0

    if max_len == 0:
        return None, None, None

    char_ids = torch.zeros((len(names), max_len), dtype=torch.long)
    for i, seq in enumerate(char_seqs):
        char_ids[i, :len(seq)] = torch.tensor(seq, dtype=torch.long)

    lang_ids = torch.tensor(lang_ids_list, dtype=torch.long)
    lengths = torch.tensor(seq_lengths, dtype=torch.long)

    return char_ids.to(device), lang_ids.to(device), lengths


def process_chunk(df, model, device, model_version):
    """Process a dataframe chunk and return with embeddings."""
    names = df["name"].fillna("").tolist()
    langs = df["lang"].fillna("unk").tolist()

    all_embeddings = []

    with torch.no_grad():
        for i in range(0, len(names), GPU_BATCH_SIZE):
            b_names = names[i:i + GPU_BATCH_SIZE]
            b_langs = langs[i:i + GPU_BATCH_SIZE]

            c_ids, l_ids, lengths = collate_batch(b_names, b_langs, model, device)

            if c_ids is not None:
                emb = model.encode_char_only(c_ids, l_ids, lengths)
                all_embeddings.extend(emb.cpu().tolist())
            else:
                all_embeddings.extend([None] * len(b_names))

    result = pd.DataFrame({
        "_id": df["_id"],
        "embedding_bilstm": all_embeddings,
        "model_version": model_version
    })

    result = result[result["embedding_bilstm"].notna()]
    return result


def main():
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Setup scratch directory
    use_scratch = args.scratch_dir is not None
    if use_scratch:
        scratch = Path(args.scratch_dir)
        scratch_in = scratch / "input"
        scratch_out = scratch / "output"
        scratch_in.mkdir(parents=True, exist_ok=True)
        scratch_out.mkdir(parents=True, exist_ok=True)
        print(f"Using local scratch: {scratch}")
    else:
        print("No scratch dir specified, using shared storage directly")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    # Load model
    kw = {"device": device}
    if args.checkpoint:
        kw["model_path"] = args.checkpoint
    model = load_phonetic_model(**kw)
    print("Model loaded")

    # Find input files
    raw_files = sorted(input_dir.glob("raw_chunk_*.parquet"))
    if not raw_files:
        print(f"No raw_chunk_*.parquet files in {input_dir}")
        return

    print(f"Processing {len(raw_files)} chunk(s)")

    for raw_file in tqdm(raw_files, desc="Chunks"):
        out_name = raw_file.name.replace("raw_chunk_", "vectors_chunk_")
        final_out = output_dir / out_name

        if final_out.exists():
            print(f"\nSkipping {raw_file.name} (output exists)")
            continue

        if use_scratch:
            # Copy input to scratch
            local_in = scratch_in / raw_file.name
            local_out = scratch_out / out_name

            print(f"\nCopying {raw_file.name} to scratch...")
            shutil.copy2(raw_file, local_in)

            df = pd.read_parquet(local_in)
            result = process_chunk(df, model, device, args.model_version)
            result.to_parquet(local_out, index=False)

            # Copy result back
            print(f"Copying {out_name} to shared storage...")
            shutil.copy2(local_out, final_out)

            # Cleanup scratch
            local_in.unlink()
            local_out.unlink()
        else:
            df = pd.read_parquet(raw_file)
            result = process_chunk(df, model, device, args.model_version)
            result.to_parquet(final_out, index=False)

        print(f"{raw_file.name} -> {out_name} ({len(result):,} vectors)")

    print("\nEmbedding complete")


if __name__ == "__main__":
    main()