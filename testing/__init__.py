"""
Testing module for phonetic similarity model evaluation.

Contains:
    - mehdie_benchmark: MEHDIE benchmark for cross-lingual toponym matching
"""

from .mehdie_benchmark import (
    MEHDIEBenchmark,
    EvaluationResult,
    TestSet,
    create_model_similarity_fn,
    detect_language,
    levenshtein_similarity,
    jaro_winkler_similarity,
)

__all__ = [
    'MEHDIEBenchmark',
    'EvaluationResult',
    'TestSet',
    'create_model_similarity_fn',
    'detect_language',
    'levenshtein_similarity',
    'jaro_winkler_similarity',
]