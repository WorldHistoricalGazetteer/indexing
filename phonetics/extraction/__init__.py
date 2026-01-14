#!/usr/bin/env python3
"""
phonetics.extraction - Memory-optimized training data generation for Symphonym.

This package provides streaming, memory-efficient generation of training data
for the Symphonym phonetic embedding model.

Modules:
    constants: Configuration constants and utility functions
    es_knn_helper: Elasticsearch KNN operations with caching
    streaming_writer: Memory-efficient Parquet writers
    generator: Main TrainingDataGenerator class
    main: CLI entry point
"""

from phonetics.extraction.constants import (
    TRAINING_NAMESPACES, RANDOM_SEED, TARGET_SAMPLES_PER_BIN,
    MIN_BIN_SIZE, MAX_OVERSAMPLE_FACTOR, PARQUET_BATCH_SIZE
)
from phonetics.extraction.es_knn_helper import ESKNNHelper
from phonetics.extraction.generator import TrainingDataGenerator
from phonetics.extraction.streaming_writer import (
    StreamingParquetWriter, MultiSplitStreamingWriter, TripletStreamingWriter
)

__all__ = [
    'TrainingDataGenerator',
    'ESKNNHelper',
    'StreamingParquetWriter',
    'MultiSplitStreamingWriter',
    'TripletStreamingWriter',
    'TRAINING_NAMESPACES',
    'RANDOM_SEED',
    'TARGET_SAMPLES_PER_BIN',
    'MIN_BIN_SIZE',
    'MAX_OVERSAMPLE_FACTOR',
    'PARQUET_BATCH_SIZE',
]