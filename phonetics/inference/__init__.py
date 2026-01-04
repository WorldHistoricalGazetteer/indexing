"""
Inference module for phonetic embeddings.

Provides:
- ToponymEncoder: High-level encoding interface
- search_similar: Vector similarity search in ES

The ES update pipeline (extract -> compute -> push) is handled
by the update_es.py CLI module.
"""

from phonetics.inference.encoder import (
    ToponymEncoder,
    search_similar,
)

__all__ = [
    'ToponymEncoder',
    'search_similar',
]