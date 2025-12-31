# processing/embed_toponyms.py

import argparse
import sys
import time
import torch
from pathlib import Path
from elasticsearch import Elasticsearch, helpers
from tqdm import tqdm

# Ensure we can import modules if running as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processing.utilities import load_phonetic_model
from processing.settings import ES_HOST, STAGING_REPO_NAME, TOPONYMS_INDEX

SOURCE_TEXT_FIELD = "name"
SOURCE_LANG_FIELD = "lang"
VECTOR_FIELD = "embedding_bilstm"
VERSION_FIELD = "model_version"

ES_SCROLL_SIZE = 10000
GPU_BATCH_SIZE = 512


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model-version", type=int, required=True,
                   help="Integer version number for this model run")
    p.add_argument("--checkpoint", type=str, default=None,
                   help="Path to .pt file (optional, defaults to utilities.py default)")
    return p.parse_args()


def ensure_version_field(es, index):
    mapping = es.indices.get_mapping(index=index)
    props = mapping[index]["mappings"].get("properties", {})

    if VERSION_FIELD not in props:
        es.indices.put_mapping(
            index=index,
            body={
                "properties": {
                    VERSION_FIELD: {"type": "integer"}
                }
            }
        )


def trigger_snapshot(es, version, wait_for_completion=False):
    """
    Triggers a snapshot.
    Default: Fire-and-forget (async) for intermediate checkpoints.
    """
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_name = f"checkpoint_embeddings_v{version}_{timestamp}"

    try:
        es.snapshot.create(
            repository=STAGING_REPO_NAME,
            snapshot=snapshot_name,
            body={
                "indices": "toponyms",  # Only snapshot what we are changing
                "ignore_unavailable": True,
                "include_global_state": False
            },
            wait_for_completion=wait_for_completion
        )
        print(f"\n[Background] Snapshot triggered: {snapshot_name}")
    except Exception as e:
        # Don't crash the job if snapshot fails (e.g., previous one still running)
        print(f"\n[Warning] Could not trigger snapshot: {e}")


def build_query(model_version):
    return {
        "bool": {
            "must": [{"match_all": {}}],
            "filter": [
                {"bool": {
                    "must_not": [
                        {"term": {VERSION_FIELD: model_version}}
                    ]
                }}
            ]
        }
    }


def collate_batch(names, langs, model, device):
    """
    Tokenizes text and languages, pads them, and moves to GPU.
    Uses the vocabs attached to the model in utilities.py.
    """
    # 1. Tokenize using the vocabs attached to the model
    # handling 'unk' if char/lang not found
    char_seqs = [model.char_vocab.encode(n) for n in names]

    # Assuming LangVocab has a similar method or dictionary look up
    # fallback to 0 if lang not found
    lang_ids_list = []
    for l in langs:
        try:
            # Try dictionary access first, then method, then fallback
            lid = model.lang_vocab.get_id(l) if hasattr(model.lang_vocab, 'get_id') else model.lang_vocab[l]
        except (KeyError, AttributeError):
            lid = 0
        lang_ids_list.append(lid)

    # 2. Pad Sequences
    seq_lengths = [len(s) for s in char_seqs]
    max_len = max(seq_lengths) if seq_lengths else 0

    if max_len == 0:
        return None, None, None

    # Create tensors (Initialize with padding index, usually 0)
    char_ids_tensor = torch.zeros((len(names), max_len), dtype=torch.long)
    for i, seq in enumerate(char_seqs):
        char_ids_tensor[i, :len(seq)] = torch.tensor(seq, dtype=torch.long)

    lang_ids_tensor = torch.tensor(lang_ids_list, dtype=torch.long)
    seq_lengths_tensor = torch.tensor(seq_lengths, dtype=torch.long)  # Keep on CPU for pack_padded

    return char_ids_tensor.to(device), lang_ids_tensor.to(device), seq_lengths_tensor


def main():
    args = parse_args()
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # Require a model_version argument
    if args.model_version is None:
        print("ERROR: --model-version is required")
        sys.exit(1)

    print(f"--- Starting Embedding Update (Version {args.model_version}) ---")
    print(f"Device: {device}")
    print(f"Elasticsearch: {ES_HOST}")

    es = Elasticsearch(ES_HOST, request_timeout=300)

    ensure_version_field(es, TOPONYMS_INDEX)

    kw_args = {"device": device}
    if args.checkpoint:
        kw_args["model_path"] = args.checkpoint

    model = load_phonetic_model(**kw_args)

    query = build_query(args.model_version)

    # Count total for progress bar
    try:
        count_resp = es.count(index=TOPONYMS_INDEX, body={"query": query})
        total_docs = count_resp['count']
        print(f"Documents to process: {total_docs}")
    except:
        total_docs = None

    # Use Scan helper for efficient deep-pagination
    scan_gen = helpers.scan(
        es,
        index=TOPONYMS_INDEX,
        query=query,
        scroll="30m",
        size=ES_SCROLL_SIZE,
        _source=[SOURCE_TEXT_FIELD, SOURCE_LANG_FIELD]
    )

    SNAPSHOT_INTERVAL = 3 * 60 * 60  # 3 hours in seconds
    last_snapshot_time = time.time()

    pbar = tqdm(total=total_docs, unit="docs")
    batch_buffer = []

    def process_buffer(buffer):
        nonlocal last_snapshot_time

        if not buffer: return

        # Unpack
        doc_ids = [d['_id'] for d in buffer]
        names = [d['_source'].get(SOURCE_TEXT_FIELD, '') for d in buffer]
        langs = [d['_source'].get(SOURCE_LANG_FIELD, 'unk') for d in buffer]

        # Inference in GPU batches
        all_embeddings = []
        with torch.no_grad():
            for i in range(0, len(names), GPU_BATCH_SIZE):
                b_names = names[i:i + GPU_BATCH_SIZE]
                b_langs = langs[i:i + GPU_BATCH_SIZE]

                c_ids, l_ids, lengths = collate_batch(b_names, b_langs, model, device)

                if c_ids is not None:
                    # Model forward pass (Student/Char encoder only)
                    emb = model.encode_char_only(c_ids, l_ids, lengths)
                    all_embeddings.extend(emb.cpu().tolist())
                else:
                    # Edge case: empty strings
                    all_embeddings.extend([None] * len(b_names))

        # Prepare Bulk Update
        actions = []
        for doc_id, emb in zip(doc_ids, all_embeddings):
            if emb is None: continue

            actions.append({
                "_op_type": "update",
                "_index": TOPONYMS_INDEX,
                "_id": doc_id,
                "doc": {
                    VECTOR_FIELD: emb,
                    VERSION_FIELD: args.model_version
                }
            })

        if actions:
            helpers.bulk(es, actions, chunk_size=2000, request_timeout=60)

        if time.time() - last_snapshot_time > SNAPSHOT_INTERVAL:
            trigger_snapshot(es, args.model_version)
            last_snapshot_time = time.time()  # Reset timer
        
        pbar.update(len(buffer))

    # Main Loop
    for hit in scan_gen:
        batch_buffer.append(hit)
        if len(batch_buffer) >= ES_SCROLL_SIZE:
            process_buffer(batch_buffer)
            batch_buffer = []

    # Final cleanup
    if batch_buffer:
        process_buffer(batch_buffer)

    pbar.close()

    print("Update complete. Creating final snapshot...")
    trigger_snapshot(es, args.model_version, wait_for_completion=True)
    print("Done.")


if __name__ == "__main__":
    main()
