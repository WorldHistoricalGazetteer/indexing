"""One implementation of the PanPhon position-binned pooling.

WHY THIS MODULE EXISTS. This pooling was four separate copies of the same
loop — `rebuild_toponyms_index.to_embedding`, `_embedding_from_packed_features`,
`precompute_neural_phonetics`, and `testing/evaluate_panphon192_mehdie` — each
with its own literal `num_bins = 8`. Changing one produced vectors of a
different width from the others, and `inference/update_es.py:722` records what
that costs: the first document to arrive sets the dynamic `dense_vector`
mapping, every document of a different width is rejected with
"Cannot update parameter [dims]", and job 11173713 lost 31,757,518 of
73,479,069 documents that way while exiting 0.

🛑 `NUM_POSITION_BINS` IS A WIRE FORMAT, NOT A TUNING KNOB. It sets the width
of `panphon_embedding` (bins x 24). The live `toponyms` index carries that
field as an indexed `dense_vector` of dims 192 — dynamically mapped, since
`schemas/toponyms.json` does not declare it — and ES cannot change `dims` on an
existing field. So raising it requires a NEW index generation, not a backfill.

WHAT THE VALUE COSTS, measured 11 Sep 2026 over 8,000 real toponyms whose
median IPA length is 13 segments (mean 15.0, p90 26):

    bins  dims   names with a collision   eff. rank (participation ratio)
       8   192        6250/8000  78.1%        2.97
      12   288        4038/8000  50.5%        4.46
      16   384        2503/8000  31.3%        6.23
      24   576         994/8000  12.4%       10.39
      48  1152         437/8000   5.5%       23.36

A "collision" is two or more IPA segments landing in one bin, where they are
AVERAGED — so at the shipped value of 8, more than three names in four lose
order information inside at least one bin. That is the mechanism behind the
ordering defect in plan-symphonym-v8.md section 62.

⚠ BUT MORE BINS DOES NOT RESCUE THIS VECTOR AS A TEACHER. `evaluation.geometry`
gates `effective_rank_min` at 40.0, and even 48 bins at 1,152 dims reaches
23.36. Section 10 retires the pooled vector outright and decision D-D removes
its only remaining job (choosing positive pairs). Raise this only if that
retirement is reversed, and never in one copy.
"""

from __future__ import annotations

import struct
from typing import Iterable, List, Optional, Sequence

#: Position bins. See the module docstring before changing it — this is the
#: width of a field in a live index, not a local choice.
NUM_POSITION_BINS = 8

#: PanPhon articulatory features per IPA segment.
FEATURES_PER_SEGMENT = 24


def pool_segments(segments: Sequence[Sequence[float]],
                  num_bins: int = NUM_POSITION_BINS) -> Optional[List[float]]:
    """Pool per-segment PanPhon features into a fixed `num_bins * 24` vector.

    Each segment is assigned to a bin by its RELATIVE position
    (`seg_idx / num_segments`) and features are averaged within the bin, so the
    vector is fixed-width whatever the name's length. Empty bins stay zero.

    Returns None for an empty input or an all-zero result — ES cosine
    similarity rejects a zero-magnitude vector, so emitting one would fail at
    index time rather than here.
    """
    n = len(segments)
    if n == 0:
        return None

    bins = [[0.0] * FEATURES_PER_SEGMENT for _ in range(num_bins)]
    counts = [0] * num_bins

    for seg_idx, seg in enumerate(segments):
        bin_idx = min(int((seg_idx / n) * num_bins), num_bins - 1)
        for i, val in enumerate(seg):
            bins[bin_idx][i] += val
        counts[bin_idx] += 1

    out: List[float] = []
    for bin_idx in range(num_bins):
        if counts[bin_idx]:
            out.extend(v / counts[bin_idx] for v in bins[bin_idx])
        else:
            out.extend([0.0] * FEATURES_PER_SEGMENT)

    return out if any(out) else None


def pool_packed(packed: bytes,
                num_bins: int = NUM_POSITION_BINS) -> Optional[List[float]]:
    """Same pooling, from the stored `panphon_features` blob.

    The blob is N x 24 float32 — 24 features per IPA SEGMENT — so its length
    varies with the name. It is NOT a ready-made vector: unpacking it directly
    is what produced the mixed-width documents described in the module
    docstring. A blob whose length is not a whole number of segments is
    rejected rather than truncated, because a truncated read is a plausible
    vector derived from a corrupt one.
    """
    if not packed:
        return None

    num_floats = len(packed) // 4
    if num_floats < FEATURES_PER_SEGMENT or num_floats % FEATURES_PER_SEGMENT:
        return None

    flat = struct.unpack(f"{num_floats}f", packed)
    n = num_floats // FEATURES_PER_SEGMENT
    segments = [flat[i * FEATURES_PER_SEGMENT:(i + 1) * FEATURES_PER_SEGMENT]
                for i in range(n)]
    return pool_segments(segments, num_bins)
