#!/usr/bin/env python
"""P1 boundary-detection probe v1 — is the admin boundary separable from field lines?

Hypothesis: the OS 'mereing' boundary is a chain of x cross-marks + dots. Those
crossings are locally DENSER ink than the thin (1-2px) solid field-line mesh, and
the x-glyph correlates with a synthetic cross template. Text is also dense, but we
know every label's location (2.67M GB1900 points) so text can be masked later.

Outputs overlays to inspect separability. Go/no-go, not a finished vectoriser.
"""
import sys
import numpy as np
from PIL import Image
import cv2
from scipy.ndimage import uniform_filter, maximum_filter

SRC = sys.argv[1] if len(sys.argv) > 1 else "parish_probe.png"
im = Image.open(SRC).convert("L")
g = np.asarray(im, dtype=np.uint8)
H, W = g.shape
print(f"[probe] {SRC} {W}x{H}")

# 1. Ink mask: dark strokes. Adaptive threshold copes with the cream paper tone.
ink = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                            cv2.THRESH_BINARY_INV, 21, 12)
ink_b = (ink > 0).astype(np.float32)
print(f"[probe] ink fraction {ink_b.mean():.3f}")

# 2. Local ink density (thin lines -> low; x-crossings/dots/text -> high).
dens = uniform_filter(ink_b, size=9)

# 3. Synthetic 'x' cross template NCC (two crossing diagonals ~13px).
T = 15
tmpl = np.zeros((T, T), np.float32)
cv2.line(tmpl, (1, 1), (T - 2, T - 2), 1.0, 2)
cv2.line(tmpl, (T - 2, 1), (1, T - 2), 1.0, 2)
tmpl -= tmpl.mean()
ncc = cv2.matchTemplate(ink_b, tmpl, cv2.TM_CCOEFF_NORMED)
ncc = np.pad(ncc, ((T//2, T//2), (T//2, T//2)), mode="constant")

# 4. Candidate mereing marks: high density AND high cross-correlation, peak-picked.
score = dens * np.clip(ncc, 0, None)
peak = (score == maximum_filter(score, size=11)) & (dens > 0.28) & (ncc > 0.30)
ys, xs = np.where(peak)
print(f"[probe] candidate mereing marks: {len(xs)}")

# --- overlays ---
def save_heat(arr, name, lo=None, hi=None):
    a = arr.astype(np.float32)
    lo = a.min() if lo is None else lo
    hi = a.max() if hi is None else hi
    n = np.clip((a - lo) / (hi - lo + 1e-6), 0, 1)
    cv2.imwrite(name, cv2.applyColorMap((n * 255).astype(np.uint8), cv2.COLORMAP_JET))

save_heat(dens, "out_density.png", 0, 0.5)
cv2.imwrite("out_ink.png", ink)

rgb = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
for x, y in zip(xs, ys):
    cv2.circle(rgb, (int(x), int(y)), 7, (0, 0, 255), 2)
cv2.imwrite("out_marks.png", rgb)
print("[probe] wrote out_density.png out_ink.png out_marks.png")
