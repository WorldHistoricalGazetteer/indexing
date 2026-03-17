# gateway/symphonym.py
"""
Symphonym model integration for the API gateway.

Loads the Symphonym v7 UniversalEncoder at startup and provides:
  - embed()       → float32 L2-normalised 128-d embedding
  - embed_byte()  → int8 quantised embedding for ES KNN queries
  - knn_query()   → Build an ES KNN query body for the toponyms index
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger("gateway.symphonym")

# Lazy-loaded singleton
_model = None


def _resolve_model_dir() -> Path:
    """
    Locate the Symphonym model directory.

    Checks (in order):
      1. SYMPHONYM_MODEL_DIR env var
      2. hf/ subdirectory of the repo (has config.json, model.safetensors, vocab/)
      3. Pitt CRC layout: config in hf/, weights in checkpoints/v7/, vocab in data/v7/vocab/
    """
    import os

    # 1. Explicit env var
    env_dir = os.getenv("SYMPHONYM_MODEL_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.exists():
            return p
        logger.warning(f"SYMPHONYM_MODEL_DIR={env_dir} does not exist, falling back")

    # 2. hf/ in the repo root
    repo_hf = Path(__file__).resolve().parent.parent / "hf"
    if (repo_hf / "config.json").exists():
        # Check if we have model weights + vocab in this dir
        has_weights = (repo_hf / "model.safetensors").exists() or (repo_hf / "final_model.pt").exists()
        has_vocab = (repo_hf / "vocab" / "char_vocab.json").exists()
        if has_weights and has_vocab:
            return repo_hf

    # 3. Pitt CRC layout — assemble a composite model directory
    #    config.json lives in hf/, weights + vocab elsewhere
    ix1 = os.getenv("IX1_BASE", "/ix1/ishi")
    data_version = os.getenv("SYMPHONYM_DATA_VERSION", "7")
    checkpoint_dir = Path(ix1) / "models" / "phonetic" / "checkpoints" / f"v{data_version}"
    vocab_dir = Path(ix1) / "models" / "phonetic" / "data" / f"v{data_version}" / "vocab"

    # If the checkpoint dir has final_model.pt and we can symlink vocab, use hf/
    # by creating symlinks into hf/ (idempotent)
    if (checkpoint_dir / "final_model.pt").exists() and vocab_dir.exists():
        # Symlink weights
        hf_model_link = repo_hf / "final_model.pt"
        if not hf_model_link.exists():
            try:
                hf_model_link.symlink_to(checkpoint_dir / "final_model.pt")
                logger.info(f"Symlinked {hf_model_link} → {checkpoint_dir / 'final_model.pt'}")
            except OSError:
                pass

        # Symlink vocab dir
        hf_vocab_link = repo_hf / "vocab"
        if not hf_vocab_link.exists():
            try:
                hf_vocab_link.symlink_to(vocab_dir)
                logger.info(f"Symlinked {hf_vocab_link} → {vocab_dir}")
            except OSError:
                pass

        return repo_hf

    logger.error(
        "Cannot locate Symphonym model. Set SYMPHONYM_MODEL_DIR or ensure "
        "hf/ has model weights + vocab/, or that Pitt CRC paths are available."
    )
    return repo_hf  # Return anyway; SymphonymModel will raise on missing files


def get_model():
    """Return the singleton SymphonymModel, loading it on first call."""
    global _model
    if _model is not None:
        return _model

    # Import the standalone inference module from hf/
    model_dir = _resolve_model_dir()
    logger.info(f"Loading Symphonym model from {model_dir}...")

    # Add hf/ to sys.path so we can import inference.py directly
    hf_dir = str(Path(__file__).resolve().parent.parent / "hf")
    if hf_dir not in sys.path:
        sys.path.insert(0, hf_dir)

    from inference import SymphonymModel
    _model = SymphonymModel(model_dir=model_dir, device="cpu")
    logger.info("Symphonym model loaded successfully")
    return _model


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def embed(name: str, lang: str = "und") -> np.ndarray:
    """
    Compute a 128-d L2-normalised float32 embedding for a toponym.

    Args:
        name: Toponym string in any script.
        lang: ISO 639-1 language code (default "und" = undetermined).

    Returns:
        numpy array of shape (128,), dtype float32
    """
    model = get_model()
    return model.embed(name, lang=lang)


def embed_batch(items: List[Tuple[str, str]]) -> np.ndarray:
    """
    Compute embeddings for multiple (name, lang) pairs.

    Returns:
        numpy array of shape (N, 128), dtype float32
    """
    model = get_model()
    return model.batch_embed(items)


def quantize_to_byte(embedding: np.ndarray) -> list[int]:
    """
    Quantize a float32 L2-normalised embedding to int8 for ES byte vector storage.

    ES `dense_vector` with `element_type: byte` expects integers in [-128, 127].
    The embeddings are L2-normalised (values in [-1, 1]), so we scale by 127.

    Args:
        embedding: (D,) or (N, D) float32 array

    Returns:
        List of ints (for a single vector) suitable for ES indexing/querying.
    """
    quantized = np.round(embedding * 127.0).clip(-128, 127).astype(np.int8)
    return quantized.tolist()


# ---------------------------------------------------------------------------
# ES KNN query builder
# ---------------------------------------------------------------------------

def build_knn_query(
    name: str,
    lang: str = "und",
    k: int = 10,
    num_candidates: int = 100,
    index: str = "toponyms",
    extra_filter: Optional[dict] = None,
) -> dict:
    """
    Build an ES KNN search body for phonetic similarity.

    Generates a Symphonym embedding for the query toponym, quantizes it
    to int8, and constructs a KNN query against the `embedding` field
    in the toponyms index.

    Args:
        name: Query toponym string.
        lang: Language code (default "und").
        k: Number of nearest neighbours to return.
        num_candidates: Number of candidates to consider per shard (higher = slower but more accurate).
        index: Target ES index name (or alias).
        extra_filter: Optional ES filter clause to pre-filter candidates (e.g. by namespace, script).

    Returns:
        Dict with "knn" and optionally other ES search body keys.
    """
    emb = embed(name, lang=lang)
    query_vector = quantize_to_byte(emb)

    knn = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": k,
        "num_candidates": num_candidates,
    }

    if extra_filter:
        knn["filter"] = extra_filter

    return {
        "knn": knn,
        "_source": ["name", "lang", "script", "namespaces", "attestations", "ipa"],
        "size": k,
    }

