#!/usr/bin/env python3
"""
Memory-efficient streaming Parquet writer.

This module provides utilities for writing large datasets to Parquet
without holding everything in memory.
"""

from pathlib import Path
from typing import Dict, Generator, List, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .constants import PARQUET_BATCH_SIZE, logger


class StreamingParquetWriter:
    """
    Write data to Parquet incrementally using generators.

    Instead of collecting all data in memory and then writing,
    this streams data through in batches, keeping memory bounded.
    """

    def __init__(self, output_path: Path, batch_size: int = PARQUET_BATCH_SIZE):
        self.output_path = output_path
        self.batch_size = batch_size
        self._writer: Optional[pq.ParquetWriter] = None
        self._schema: Optional[pa.Schema] = None
        self._rows_written = 0

    def write_from_generator(self, data_gen: Generator[Dict, None, None]) -> int:
        """
        Write all items from a generator to Parquet.

        Args:
            data_gen: Generator yielding dictionaries (one per row)

        Returns:
            Total number of rows written
        """
        buffer: List[Dict] = []

        for item in data_gen:
            buffer.append(item)
            if len(buffer) >= self.batch_size:
                self._write_batch(buffer)
                buffer = []

        # Write remaining items
        if buffer:
            self._write_batch(buffer)

        self.close()
        return self._rows_written

    def _write_batch(self, batch: List[Dict]):
        """Write a batch of records to Parquet."""
        if not batch:
            return

        table = pa.Table.from_pylist(batch)

        if self._writer is None:
            self._schema = table.schema
            self._writer = pq.ParquetWriter(str(self.output_path), self._schema)

        self._writer.write_table(table)
        self._rows_written += len(batch)

    def close(self):
        """Close the writer."""
        if self._writer:
            self._writer.close()
            self._writer = None


class MultiSplitStreamingWriter:
    """
    Stream data to multiple Parquet files (train/val/test splits).

    Buffers data per split and writes incrementally.
    """

    def __init__(self, output_dir: Path, batch_size: int = PARQUET_BATCH_SIZE):
        self.output_dir = output_dir
        self.batch_size = batch_size
        self._writers: Dict[str, pq.ParquetWriter] = {}
        self._buffers: Dict[str, List[Dict]] = {}
        self._schemas: Dict[str, pa.Schema] = {}
        self._counts: Dict[str, int] = {}

    def add_sample(self, split: str, sample: Dict):
        """Add a sample to the appropriate split buffer."""
        if split not in self._buffers:
            self._buffers[split] = []
            self._counts[split] = 0

        self._buffers[split].append(sample)

        if len(self._buffers[split]) >= self.batch_size:
            self._flush_buffer(split)

    def _flush_buffer(self, split: str):
        """Write buffered samples for a split to Parquet."""
        buffer = self._buffers.get(split, [])
        if not buffer:
            return

        table = pa.Table.from_pylist(buffer)

        if split not in self._writers:
            self._schemas[split] = table.schema
            split_dir = self.output_dir / f'split={split}'
            split_dir.mkdir(parents=True, exist_ok=True)
            parquet_path = split_dir / 'data.parquet'
            self._writers[split] = pq.ParquetWriter(str(parquet_path), table.schema)

        self._writers[split].write_table(table)
        self._counts[split] += len(buffer)
        self._buffers[split] = []

    def flush_all(self):
        """Flush all buffers."""
        for split in list(self._buffers.keys()):
            self._flush_buffer(split)

    def close_all(self):
        """Close all writers."""
        self.flush_all()
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()

    def get_counts(self) -> Dict[str, int]:
        """Get counts per split."""
        return dict(self._counts)


class TripletStreamingWriter:
    """
    Specialized streaming writer for triplets with train/val splits.

    Handles the split determination based on anchor_id hash and
    writes incrementally to avoid memory issues.
    """

    def __init__(self, output_dir: Path, batch_size: int = PARQUET_BATCH_SIZE):
        self.output_dir = output_dir
        self.batch_size = batch_size
        self._train_buffer: List[Dict] = []
        self._val_buffer: List[Dict] = []
        self._train_writer: Optional[pq.ParquetWriter] = None
        self._val_writer: Optional[pq.ParquetWriter] = None
        self._schema: Optional[pa.Schema] = None
        self._train_count = 0
        self._val_count = 0

    def add_triplet(self, triplet: Dict, split_hash: int):
        """
        Add a triplet to the appropriate buffer based on hash.

        Args:
            triplet: The triplet dictionary
            split_hash: Hash value (0 = val, 1-9 = train)
        """
        if split_hash == 0:
            self._val_buffer.append(triplet)
            if len(self._val_buffer) >= self.batch_size:
                self._flush_val()
        else:
            self._train_buffer.append(triplet)
            if len(self._train_buffer) >= self.batch_size:
                self._flush_train()

    def _flush_train(self):
        if not self._train_buffer:
            return
        self._write_batch(self._train_buffer, 'train')
        self._train_count += len(self._train_buffer)
        self._train_buffer = []

    def _flush_val(self):
        if not self._val_buffer:
            return
        self._write_batch(self._val_buffer, 'val')
        self._val_count += len(self._val_buffer)
        self._val_buffer = []

    def _write_batch(self, batch: List[Dict], split: str):
        table = pa.Table.from_pylist(batch)

        if self._schema is None:
            self._schema = table.schema

        if split == 'train':
            if self._train_writer is None:
                path = self.output_dir / 'train.parquet'
                self._train_writer = pq.ParquetWriter(str(path), self._schema)
            self._train_writer.write_table(table)
        else:
            if self._val_writer is None:
                path = self.output_dir / 'val.parquet'
                self._val_writer = pq.ParquetWriter(str(path), self._schema)
            self._val_writer.write_table(table)

    def close(self) -> tuple:
        """Close writers and return counts."""
        self._flush_train()
        self._flush_val()

        if self._train_writer:
            self._train_writer.close()
        if self._val_writer:
            self._val_writer.close()

        return self._train_count, self._val_count