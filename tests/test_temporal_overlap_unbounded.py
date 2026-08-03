"""Null-aware interval overlap (place#169).

``temporal_range`` carries None for a side no timespan bounds — an open-start
boundary (``ukhc``, ``kain_par``) or an ongoing one (``un``). The rule: an
unknown bound adopts the other record's on that side, so it neither adds nor
removes span.

These fixtures are the **parity set**: the same pairs and expected values are
asserted against the browser twin, ``temporalOverlap`` in whg3's
``clustering.js``, which must score as this calibrated.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import unittest

from clustering.calibrate_params import temporal_overlap

#: (a, b, expected) — kept in a module constant so the JS parity check can read
#: the identical fixtures out of this file rather than drifting from a copy.
CASES = [
    # --- both fully bounded: unchanged from the original Jaccard ------------
    ([1400, 1550], [1400, 1550], 1.0),
    ([1400, 1500], [1450, 1550], 50 / 150),
    ([1200, 1250], [1800, 1850], 0.0),          # disjoint
    ([1851, 1851], [1851, 1851], 1.0),          # same single year
    # --- one side open ------------------------------------------------------
    ([None, 1974], [1889, 1974], 1.0),          # nothing distinguishes them
    ([None, 1974], [1200, 1250], 50 / 774),     # what IS known barely coincides
    ([1707, None], [1800, 1850], 50 / 143),
    ([None, 1974], [1980, 1990], 0.0),          # known end precedes the other start
    # --- both open on the same side ----------------------------------------
    ([None, 1974], [None, 1974], 1.0),          # identical claims
    ([None, 1974], [None, 1850], 0.0),          # start side contributes nothing
    ([1707, None], [1707, None], 1.0),
    # --- undated ------------------------------------------------------------
    (None, [1400, 1550], 0.0),
    ([1400, 1550], None, 0.0),
    ([None, None], [1400, 1550], 0.0),
]


class TestTemporalOverlapUnbounded(unittest.TestCase):
    def test_cases(self):
        for a, b, expected in CASES:
            with self.subTest(a=a, b=b):
                self.assertAlmostEqual(temporal_overlap(a, b), expected, places=9)

    def test_symmetric(self):
        for a, b, _ in CASES:
            with self.subTest(a=a, b=b):
                self.assertAlmostEqual(temporal_overlap(a, b), temporal_overlap(b, a),
                                       places=9)

    def test_open_start_is_not_penalised_for_an_early_window(self):
        # The flicker place#169 fixed: ukhc:KNT used to arrive as [1974, 1974],
        # scoring 0 against anything medieval purely because its start was unknown.
        open_start = [None, 1974]
        collapsed = [1974, 1974]
        medieval = [1200, 1400]
        self.assertEqual(temporal_overlap(collapsed, medieval), 0.0)
        self.assertGreater(temporal_overlap(open_start, medieval), 0.0)

    def test_bounded_within_unbounded_scores_higher_than_disjoint(self):
        contained = temporal_overlap([None, 1974], [1900, 1974])
        marginal = temporal_overlap([None, 1974], [1200, 1250])
        self.assertGreater(contained, marginal)

    def test_js_twin_scores_identically(self):
        """Run the same fixtures through whg3's ``temporalOverlap``.

        The two implementations must not drift: the browser scores as this
        calibrated. Skips where the whg3 checkout or node isn't present (set
        ``WHG3_PATH`` if it lives somewhere other than the default).
        """
        whg3 = pathlib.Path(os.environ.get(
            "WHG3_PATH", pathlib.Path.home() / "Documents/GitHub/whg3"))
        source = whg3 / "whg/webpack/js/clustering.js"
        if not source.exists():
            self.skipTest(f"whg3 clustering.js not found at {source}")
        if shutil.which("node") is None:
            self.skipTest("node not available")

        src = source.read_text(encoding="utf-8")
        start = src.index("export function temporalOverlap")
        end = src.index("\n}", start) + 2
        body = src[start:end].replace("export function", "function")

        script = (
            f"{body}\n"
            f"const cases = {json.dumps(CASES)};\n"
            "let bad = 0;\n"
            "for (const [a, b, expected] of cases) {\n"
            "  const got = temporalOverlap(a, b);\n"
            "  if (Math.abs(got - expected) > 1e-9) { bad++; "
            "console.log('MISMATCH', JSON.stringify(a), JSON.stringify(b), got, expected); }\n"
            "}\n"
            "console.log(bad === 0 ? 'PARITY_OK' : 'PARITY_FAIL');\n"
        )
        out = subprocess.run(["node", "--input-type=module", "-e", script],
                             capture_output=True, text=True, timeout=60)
        self.assertIn("PARITY_OK", out.stdout,
                      f"whg3 temporalOverlap diverged:\n{out.stdout}{out.stderr}")


if __name__ == "__main__":
    unittest.main()
