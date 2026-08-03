# processing/utilities.py

import gzip
import time
import zipfile
from pathlib import Path

# import torch
# from phonetics.models import HybridPhoneticModel, PhoneticEncoder, CharEncoder
# from phonetics.vocab import CharVocab, LangVocab

from processing.settings import STAGING_REPO_NAME, PLACES_INDEX, TOPONYMS_INDEX, IX1_BASE


# def load_phonetic_model(model_path: str = f"{IX1_BASE}/models/phonetic/checkpoints/final_model_b.pt", device: str = "cuda"):
#     """
#     Load the Hybrid BiLSTM phonetic embedding model for inference.
#     """
#
#     model_path = Path(model_path)
#     vocab_dir = model_path.parent
#     base_name = model_path.stem
#
#     char_vocab = CharVocab.load(vocab_dir / f"{base_name}_char_vocab.pkl")
#     lang_vocab = LangVocab.load(vocab_dir / f"{base_name}_lang_vocab.pkl")
#
#     checkpoint = torch.load(model_path, map_location="cpu")
#
#     phonetic_encoder = PhoneticEncoder()
#
#     char_encoder = CharEncoder(
#         vocab_size=checkpoint.get("char_vocab_size", char_vocab.vocab_size),
#         num_langs=checkpoint.get("num_langs", lang_vocab.next_id),
#     )
#
#     model_cfg = checkpoint.get("model_cfg", {})
#
#     model = HybridPhoneticModel(
#         phonetic_encoder,
#         char_encoder,
#         **model_cfg
#     )
#
#     state = checkpoint.get("model_state", checkpoint)
#     model.load_state_dict(state, strict=True)
#
#     model.char_vocab = char_vocab
#     model.lang_vocab = lang_vocab
#
#     model.to(device)
#     model.eval()
#
#     return model


def stream_file(file_path, member=None):
    """
    Generator yielding lines from .txt, .gz, or .zip files.
    ZIP files are streamed without extraction.

    ``member`` names the single archive entry to read. Pass it whenever the
    archive holds more than one ``.txt``: this used to concatenate every
    ``.txt`` member, on the stated assumption that there was "only one relevant
    file in the zip (like alternateNamesV2.txt)" — and that is exactly the
    archive where it is false. ``alternateNamesV2.zip`` also carries
    ``iso-languagecodes.txt``, whose rows were parsed as alternate names and
    emitted 415 patch rows for place_ids like ``gn:aar``, ``gn:ISO 639-2`` and
    ``gn:``. They matched nothing, so the damage was only noise — but nothing
    said so, and a language code that happened to collide with a real id would
    have written a name onto the wrong place.

    Without ``member``, a multi-``.txt`` archive now warns rather than silently
    concatenating.
    """
    if file_path.endswith(".gz"):
        with gzip.open(file_path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")

    elif file_path.endswith(".zip"):
        with zipfile.ZipFile(file_path, 'r') as zf:
            texts = [n for n in zf.namelist() if n.endswith('.txt')]
            if member is not None:
                if member not in texts:
                    raise FileNotFoundError(
                        f"{member!r} not in {file_path} (has: {texts})"
                    )
                texts = [member]
            elif len(texts) > 1:
                print(f"  WARNING: {file_path} holds {len(texts)} .txt members "
                      f"{texts} and no `member` was given — reading all of them, "
                      f"which is almost certainly not intended.")
            for name in texts:
                with zf.open(name, 'r') as f:
                    for line in f:
                        yield line.decode('utf-8').strip()

    else:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")


def create_checkpoint_snapshot(es, snapshot_name="checkpoint", repo_name=STAGING_REPO_NAME):
    """
    Create a checkpoint snapshot after completing a logical unit of work.

    Args:
        es: Elasticsearch client
        snapshot_name: Name for the snapshot (will be prefixed with timestamp)
        repo_name: Snapshot repository name

    Returns:
        dict with snapshot info or None on failure
    """
    from datetime import datetime
    import time

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    full_name = f"{snapshot_name}_{timestamp}"

    indices = [PLACES_INDEX, TOPONYMS_INDEX]

    print(f"\nCreating checkpoint snapshot: {full_name}")
    print(f"  Indices: {', '.join(indices)}")

    try:
        # 1. Start snapshot asynchronously (wait_for_completion=False)
        es.snapshot.create(
            repository=repo_name,
            snapshot=full_name,
            body={
                "indices": ",".join(indices),
                "ignore_unavailable": True,
                "include_global_state": False
            },
            wait_for_completion=False
        )

        # 2. Poll for completion
        print(f"  Snapshot in progress...", end="", flush=True)

        final_status = None
        while True:
            try:
                # Get specific snapshot status
                status = es.snapshot.get(repository=repo_name, snapshot=full_name)

                # Check if snapshot info is available yet
                if not status.get('snapshots'):
                    time.sleep(2)
                    continue

                snapshot_info = status['snapshots'][0]
                state = snapshot_info.get("state")

                if state == "SUCCESS":
                    final_status = snapshot_info
                    print(" Done.")  # Clean newline
                    break
                elif state in ["FAILED", "PARTIAL", "INCOMPATIBLE"]:
                    final_status = snapshot_info
                    print(f"\n  ✗ Snapshot finished with state: {state}")
                    break

                # If IN_PROGRESS, print dot and wait
                print(".", end="", flush=True)
                time.sleep(5)  # Check every 5 seconds

            except Exception as e:
                # If network blip, just retry loop
                print(f"!", end="", flush=True)
                time.sleep(5)

        # 3. Report result
        if final_status and final_status.get("state") == "SUCCESS":
            print(f"  ✓ Snapshot created successfully: {full_name}")
            return final_status
        else:
            print(f"  ✗ Snapshot failed.")
            return None

    except Exception as e:
        print(f"\n  ✗ Snapshot request failed: {e}")
        return None
