"""Contract tests for the order negative.

The guarantee that matters is not "it shuffles" but that it can never emit a
string `apply_character_noise` would produce as a POSITIVE. That function's
`transpose` edit swaps adjacent characters and is applied to anchors as noise,
so a permutation negative that is a single adjacent swap would be taught as
both a positive and a negative.
"""
import collections
import random
import unittest

from phonetics.training.data_loading import permutation_negative


def _single_adjacent_swap(a: str, b: str) -> bool:
    """True if b is a exactly with one adjacent pair transposed."""
    if len(a) != len(b):
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diff) != 2:
        return False
    i, j = diff
    return j == i + 1 and a[i] == b[j] and a[j] == b[i]


class PermutationNegative(unittest.TestCase):

    NAMES = ["London", "Varanasi", "Mawdsley", "Broxbourne", "Jaipur",
             "Bury St Edmunds", "Caojiawopu", "Abu Gharab"]

    def setUp(self):
        random.seed(20260911)

    def test_refuses_when_no_informative_permutation_exists(self):
        # None means "use the corpus negative". An empty string here would be
        # encoded as a real training example of nothing.
        for text in ("", "a", "ab", "abc", "aaaa", "aaaaaaa"):
            with self.subTest(text=text):
                self.assertIsNone(permutation_negative(text))

    def test_preserves_the_character_multiset(self):
        # The point of an anagram negative: content held constant so that the
        # ONLY thing distinguishing it from the anchor is order.
        for name in self.NAMES:
            with self.subTest(name=name):
                out = permutation_negative(name)
                self.assertIsNotNone(out)
                self.assertEqual(collections.Counter(out), collections.Counter(name))

    def test_never_returns_the_input(self):
        for name in self.NAMES:
            for _ in range(50):
                self.assertNotEqual(permutation_negative(name), name)

    def test_never_a_single_adjacent_swap(self):
        # The anti-contradiction guard, over enough trials to catch a rare one.
        for name in self.NAMES:
            for _ in range(200):
                out = permutation_negative(name)
                if out is None:
                    continue
                self.assertFalse(
                    _single_adjacent_swap(name, out),
                    f"{name!r} -> {out!r} is a typo, taught elsewhere as a positive")

    def test_displacement_floor_is_honoured(self):
        for name in self.NAMES:
            for _ in range(50):
                out = permutation_negative(name, min_displaced_ratio=0.5)
                if out is None:
                    continue
                displaced = sum(1 for a, b in zip(name, out) if a != b)
                self.assertGreaterEqual(displaced, max(2, int(len(name) * 0.5)))

    def test_a_stricter_floor_is_actually_stricter(self):
        # If the parameter were ignored this test would pass trivially at 0.9
        # while the implementation shuffled to any displacement at all.
        name = "Broxbourne"
        for _ in range(50):
            out = permutation_negative(name, min_displaced_ratio=0.9)
            if out is None:
                continue
            displaced = sum(1 for a, b in zip(name, out) if a != b)
            self.assertGreaterEqual(displaced, 9)


if __name__ == "__main__":
    unittest.main()
