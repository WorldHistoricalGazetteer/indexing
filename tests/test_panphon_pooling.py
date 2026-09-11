"""Contract tests for the shared PanPhon pooling.

These assert what the pooling must GUARANTEE, not what its current body does.
A test written the other way round — asserting the literal 192 and the
current loop — goes green on exactly the defect this module was created to
prevent: one copy drifting to a different width from another.
"""
import struct
import unittest

from phonetics.panphon_pooling import (
    FEATURES_PER_SEGMENT, NUM_POSITION_BINS, pool_packed, pool_segments)


def _segments(n, value=lambda i: float(i + 1)):
    return [[value(i)] * FEATURES_PER_SEGMENT for i in range(n)]


def _pack(segments):
    flat = [v for seg in segments for v in seg]
    return struct.pack(f"{len(flat)}f", *flat)


class PoolingContract(unittest.TestCase):

    def test_width_is_bins_times_features_whatever_the_name_length(self):
        # The point of pooling: a fixed width from a variable-length name.
        # A width that varies with the name is what got 31,757,518 documents
        # rejected by a dynamically-mapped dense_vector.
        for n in (1, 2, 7, 13, 40, 132):
            with self.subTest(segments=n):
                self.assertEqual(len(pool_segments(_segments(n))),
                                 NUM_POSITION_BINS * FEATURES_PER_SEGMENT)

    def test_both_derivations_agree(self):
        # The packed path and the segment path are two entries to one
        # computation; if they can disagree, stored and freshly computed
        # vectors occupy different spaces while both look valid.
        for n in (1, 5, 13, 64):
            with self.subTest(segments=n):
                segs = _segments(n)
                self.assertEqual(pool_segments(segs), pool_packed(_pack(segs)))

    def test_order_changes_the_vector(self):
        # The whole reason this vector is binned by position rather than
        # averaged flat. If reversal leaves it unchanged, order is gone.
        segs = _segments(13)
        self.assertNotEqual(pool_segments(segs), pool_segments(list(reversed(segs))))

    def test_empty_bins_are_zero_not_dropped(self):
        # A short name must still fill the full width, with the unoccupied
        # bins zeroed — dropping them would make the width vary again.
        v = pool_segments(_segments(2))
        self.assertEqual(len(v), NUM_POSITION_BINS * FEATURES_PER_SEGMENT)
        self.assertIn(0.0, v)

    def test_nothing_in_nothing_out(self):
        # An all-zero vector is rejected by ES cosine similarity, so it must
        # not be emitted as if it were a result.
        self.assertIsNone(pool_segments([]))
        self.assertIsNone(pool_packed(b""))
        self.assertIsNone(pool_segments([[0.0] * FEATURES_PER_SEGMENT]))

    def test_ragged_blob_is_refused_not_truncated(self):
        # A blob that is not a whole number of segments is corrupt. Truncating
        # it yields a plausible vector from bad bytes, which is worse than None.
        self.assertIsNone(pool_packed(b"\x00" * 100))

    def test_bin_count_is_honoured(self):
        # The sweep in plan-symphonym-v8.md section 63 varies this; if the
        # parameter were ignored the sweep would measure one thing repeatedly.
        segs = _segments(13)
        self.assertEqual(len(pool_segments(segs, 16)), 16 * FEATURES_PER_SEGMENT)
        self.assertNotEqual(pool_segments(segs, 16)[:FEATURES_PER_SEGMENT],
                            pool_segments(segs, 8)[:FEATURES_PER_SEGMENT])


if __name__ == "__main__":
    unittest.main()
