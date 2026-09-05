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
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .bounded_io import IoGuard

logger = logging.getLogger("gateway.symphonym")

# Lazy-loaded singleton
_model = None


# ---------------------------------------------------------------------------
# Bounded filesystem access (place#242)
# ---------------------------------------------------------------------------
#
# Every probe below can traverse a hard NFS mount — `hf/final_model.pt` and
# `hf/vocab` are symlinks into IX1_BASE on the CRC deployment, planted by this
# module's own CRC-layout branch — and `Path.exists()` follows a symlink. A hard
# mount that wedges does not raise; it blocks in uninterruptible sleep. That took
# the gateway down for ~13 minutes on 5 Sep 2026: both workers sat in `D` state
# inside `app.lifespan`'s "won't crash if unavailable" try/except, which catches
# everything that RAISES and so caught nothing, and 9200 never opened.
#
# So resolution is bounded by a clock rather than by an exception handler, and an
# unreachable candidate is *skipped* — never fatal, never blocking. See
# `gateway/bounded_io.py` (place#241, the same defect in the query path).

# One probe is a single stat(); a second is already pathological.
_PROBE_TIMEOUT_S = float(os.getenv("SYMPHONYM_PROBE_TIMEOUT_S", "2.0"))
# The load reads ~33-99 MB and legitimately takes ~1.8s from /vast flash, so this
# is generous by design: it exists to make an unreachable store terminate, not to
# police a slow one.
_LOAD_TIMEOUT_S = float(os.getenv("SYMPHONYM_LOAD_TIMEOUT_S", "60.0"))
# Startup-path resolution is not a hot loop; a short cooldown keeps a retry cheap
# without writing the model off for the life of the process.
_IO_COOLDOWN_S = float(os.getenv("SYMPHONYM_IO_COOLDOWN_S", "10.0"))

# ONE GUARD PER CANDIDATE, never one shared across all of them. A guard's
# breaker writes its resource off for a cooldown after a timeout — which is the
# point — so a single shared guard would let a wedged /ix1 probe suppress the
# probes of the healthy /vast candidate *behind* it, and resolution would fail
# with the model sitting right there on flash. Same reasoning as the per-store
# guards in `hard_link_expansion`: these are different filesystems and one being
# down says nothing about another.
_PROBE_GUARDS: dict[str, IoGuard] = {}

LOAD_GUARD = IoGuard("symphonym model load",
                     timeout=_LOAD_TIMEOUT_S, cooldown=_IO_COOLDOWN_S)


def _guard(candidate: str) -> IoGuard:
    """The probe guard for one candidate location, created on first use."""
    guard = _PROBE_GUARDS.get(candidate)
    if guard is None:
        guard = IoGuard(f"symphonym probe [{candidate}]",
                        timeout=_PROBE_TIMEOUT_S, cooldown=_IO_COOLDOWN_S)
        _PROBE_GUARDS[candidate] = guard
    return guard

#: Candidates skipped because the filesystem did not answer, as opposed to
#: because they were absent. Reported by :func:`status` and surfaced on
#: ``/api/health`` — "the model is missing" and "the model store is unreachable"
#: are different operational problems and must not read alike.
_unreachable: list[str] = []


def _exists(path: Path) -> bool:
    """Raw existence test. The single seam through which this module touches the
    filesystem, so bounding it bounds everything (and tests can block it)."""
    return path.exists()


def _probe(path: Path, candidate: str) -> str:
    """``present`` | ``absent`` | ``unreachable`` — a bounded :func:`_exists`.

    ``unreachable`` is the case that previously had no name and no exit: the
    filesystem neither confirmed nor denied the path, and the caller waited
    forever. It is recorded, not raised, so resolution can try the next
    candidate. ``candidate`` selects that candidate's own guard, so a wedge in
    one location cannot suppress the probes of another."""
    outcome = _guard(candidate).run(_exists, path, default=None)
    if outcome.ok:
        return "present" if outcome.value else "absent"
    if outcome.status in ("timeout", "tripped", "saturated"):
        entry = str(path)
        if entry not in _unreachable:
            _unreachable.append(entry)
        logger.warning("symphonym: %s did not respond (%s) — skipping this "
                       "candidate rather than waiting on it", path, outcome.status)
        return "unreachable"
    return "absent"  # a real error (permissions, bad path) — treat as not there


def _crc_layout_dir(base: str, repo_hf: Path, data_version: str,
                    candidate: str) -> Path | None:
    """Resolve the CRC split layout under ``base``, or None if it isn't there.

    ``config.json`` ships in the repo's ``hf/`` while the weights and vocab live
    under ``<base>/models/phonetic/``, so the two are married by planting
    symlinks into ``hf/``. Those symlinks are exactly how ``hf/`` came to point
    into a hard mount (place#242) — so they are now planted against **whichever
    base actually resolved**, and ``IX3_BASE`` is tried before ``IX1_BASE`` by
    the caller. The code no longer rewires its own fast path onto slow storage.
    """
    checkpoint_dir = Path(base) / "models" / "phonetic" / "checkpoints" / f"v{data_version}"
    vocab_dir = Path(base) / "models" / "phonetic" / "data" / f"v{data_version}" / "vocab"

    if _probe(checkpoint_dir / "final_model.pt", candidate) != "present":
        return None
    if _probe(vocab_dir, candidate) != "present":
        return None

    for link, target in ((repo_hf / "final_model.pt", checkpoint_dir / "final_model.pt"),
                         (repo_hf / "vocab", vocab_dir)):
        # `absent` only — never overwrite, and never treat `unreachable` as
        # absent, or a wedged mount would provoke a symlink over a live one.
        if _probe(link, "repo-hf") != "absent":
            continue
        try:
            link.symlink_to(target)
            logger.info("symphonym: symlinked %s -> %s", link, target)
        except OSError as exc:
            logger.warning("symphonym: could not symlink %s -> %s: %s", link, target, exc)
    return repo_hf


def _resolve_model_dir(repo_hf: Path | None = None) -> Path:
    """Locate the Symphonym model directory.

    Checks, in order, skipping any candidate the filesystem does not answer for
    within :data:`_PROBE_TIMEOUT_S`:

      1. ``SYMPHONYM_MODEL_DIR``
      2. ``hf/`` in the repo (self-contained: config + weights + vocab/)
      3. CRC split layout under ``IX3_BASE`` (/vast flash) — preferred
      4. CRC split layout under ``IX1_BASE`` — last, because it is the hard
         mount whose wedge is the whole point of place#242

    Returns the best candidate; when none resolves it still returns ``hf/`` so
    ``SymphonymModel`` raises a legible error, exactly as before. What has
    changed is that getting to that error now takes seconds instead of never.

    ``repo_hf`` defaults to the repo's own ``hf/`` and exists so a test can point
    the whole search at a temp tree — the wedged-mount case cannot be exercised
    against the real one.
    """
    _unreachable.clear()

    # 1. Explicit env var — an operator override, and the documented escape
    #    hatch when the shared filesystems are misbehaving.
    env_dir = os.getenv("SYMPHONYM_MODEL_DIR")
    if env_dir:
        p = Path(env_dir)
        state = _probe(p, "env")
        if state == "present":
            return p
        logger.warning("SYMPHONYM_MODEL_DIR=%s is %s, falling back", env_dir, state)

    # 2. hf/ in the repo root, self-contained.
    if repo_hf is None:
        repo_hf = Path(__file__).resolve().parent.parent / "hf"
    if _probe(repo_hf / "config.json", "repo-hf") == "present":
        has_weights = (_probe(repo_hf / "model.safetensors", "repo-hf") == "present"
                       or _probe(repo_hf / "final_model.pt", "repo-hf") == "present")
        has_vocab = _probe(repo_hf / "vocab" / "char_vocab.json", "repo-hf") == "present"
        if has_weights and has_vocab:
            return repo_hf

    # 3/4. CRC split layout. IX3_BASE (flash) FIRST — a serving path should not
    #      default to the hard mount, and whichever base wins is the one whose
    #      paths get symlinked into hf/ for next time.
    data_version = os.getenv("SYMPHONYM_DATA_VERSION", "7")
    for candidate, base in (("ix3", os.getenv("IX3_BASE", "/vast/ishi")),
                            ("ix1", os.getenv("IX1_BASE", "/ix1/ishi"))):
        resolved = _crc_layout_dir(base, repo_hf, data_version, candidate)
        if resolved is not None:
            return resolved

    if _unreachable:
        logger.error(
            "Cannot locate Symphonym model: %d candidate path(s) did not respond "
            "(%s). This is an UNREACHABLE store, not a missing one — the model may "
            "be perfectly intact behind a wedged mount. Set SYMPHONYM_MODEL_DIR to "
            "a copy on healthy storage.", len(_unreachable), ", ".join(_unreachable))
    else:
        logger.error(
            "Cannot locate Symphonym model. Set SYMPHONYM_MODEL_DIR or ensure "
            "hf/ has model weights + vocab/, or that the CRC paths are available.")
    return repo_hf  # Return anyway; SymphonymModel will raise on missing files


def status() -> dict:
    """Model availability, for ``/api/health``.

    Distinguishes *loaded*, *not yet loaded*, and *unreachable*. The last is the
    state place#242 could not report at all, because the process was blocked in
    a syscall instead of running code that could describe itself.
    """
    return {
        "loaded": _model is not None,
        "unreachable_paths": list(_unreachable),
        "probes": [g.stats() for g in _PROBE_GUARDS.values()],
        "load": LOAD_GUARD.stats(),
    }


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

    # The load itself reads the weights, so it has the same exposure as the
    # probes: on a wedged mount `torch.load` blocks and never returns. Bounded
    # for the same reason, and RAISES on expiry so the caller gets a fast, legible
    # failure — `app.lifespan` logs it and the gateway comes up without phonetic
    # search, instead of never coming up at all.
    outcome = LOAD_GUARD.run(SymphonymModel, model_dir=model_dir, device="cpu")
    if not outcome.ok:
        if outcome.status in ("timeout", "tripped", "saturated"):
            raise TimeoutError(
                f"Symphonym model at {model_dir} did not load within "
                f"{_LOAD_TIMEOUT_S:.0f}s ({outcome.status}) — the store is "
                f"unreachable, not absent.")
        raise RuntimeError(f"Symphonym model at {model_dir} failed to load")
    _model = outcome.value
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
    query_vector: Optional[list[int]] = None,
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
    # A caller-supplied vector (already int8-quantised, e.g. from the browser Symphonym model)
    # lets us skip the server-side embed — the client offloads that cost. Fall back to embedding
    # the query text here when none is provided.
    if query_vector is None:
        query_vector = quantize_to_byte(embed(name, lang=lang))

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
        "_source": ["name", "lang", "script", "namespaces", "attestations"],
        "size": k,
    }

