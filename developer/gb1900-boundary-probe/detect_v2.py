#!/usr/bin/env python
"""P1 probe v2 — the boundary-SPECIFIC cue: bold x mereing-marks.

Field lines/contours/tidal-dot lines have NO bold x. Isolate BOLD ink (distance
transform kills 1-2px strokes), then match a bold-x template. If the surviving
hits trace the admin boundary and stay quiet on the field mesh, classical
anchor-detection + label-seeded tracing is viable.
"""
import sys
import numpy as np
from PIL import Image
import cv2
from scipy.ndimage import maximum_filter

SRC = sys.argv[1] if len(sys.argv) > 1 else "parish_probe.png"
g = np.asarray(Image.open(SRC).convert("L"), np.uint8)
H, W = g.shape

ink = (cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                             cv2.THRESH_BINARY_INV, 21, 12) > 0).astype(np.uint8)

# BOLD ink = stroke half-width >= 2px (thickness >=~4px). Field lines ~1-2px vanish.
dist = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
bold = (dist >= 1.8).astype(np.float32)
# dilate bold cores back to full glyph extent
bold = cv2.dilate(bold, np.ones((3, 3), np.uint8))
print(f"[probe] ink frac {ink.mean():.3f}  bold frac {bold.mean():.4f}")

# bold-x template (thick arms)
T = 19
tmpl = np.zeros((T, T), np.float32)
cv2.line(tmpl, (2, 2), (T - 3, T - 3), 1.0, 4)
cv2.line(tmpl, (T - 3, 2), (2, T - 3), 1.0, 4)
tmpl -= tmpl.mean()
ncc = cv2.matchTemplate(bold, tmpl, cv2.TM_CCOEFF_NORMED)
ncc = np.pad(ncc, ((T // 2, T // 2), (T // 2, T // 2)))

peak = (ncc == maximum_filter(ncc, size=15)) & (ncc > 0.45)
ys, xs = np.where(peak)
print(f"[probe] bold-x candidates: {len(xs)} (thr 0.45)")

# overlays
cv2.imwrite("out_bold.png", (bold * 255).astype(np.uint8))
rgb = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
for x, y in zip(xs, ys):
    cv2.circle(rgb, (int(x), int(y)), 10, (0, 0, 255), 2)
cv2.imwrite("out_boldx.png", rgb)
print("[probe] wrote out_bold.png out_boldx.png")
