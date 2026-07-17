#!/usr/bin/env python
"""Realistic print+scan degradation for synthetic map crops (blotchy/blurry print).

Plug-in: degrade(bg_float, ink_layer_float[0..1], dark) -> uint8 image.
Models the letterpress/engraved-ink + scan artefacts the earlier synth lacked:
uneven ink density, broken/dropped strokes, ink bleed, foxing spots, blur, speckle.
"""
import numpy as np, cv2

def _lowfreq(shape, rng, scale):
    """smooth random field in [0,1]."""
    h, w = shape
    small = rng.random((max(2, h // scale), max(2, w // scale))).astype(np.float32)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def degrade(bg, ink, dark, rng):
    h, w = ink.shape
    # 1. uneven ink density: modulate ink amount by low+high freq fields
    dens = 0.55 + 0.5 * _lowfreq((h, w), rng, rng.integers(6, 16))          # 0.55..1.05
    dens *= (0.85 + 0.15 * rng.random((h, w)).astype(np.float32))           # fine grain
    ink_e = np.clip(ink * dens, 0, 1)
    # 2. broken strokes: drop ink where a smooth mask is low (failed transfer)
    if rng.random() < 0.8:
        drop = _lowfreq((h, w), rng, rng.integers(4, 10))
        ink_e *= (drop > rng.uniform(0.15, 0.4)).astype(np.float32)
    # 3. ink bleed: faint dilation halo
    if rng.random() < 0.6:
        bleed = cv2.dilate(ink_e, np.ones((3, 3), np.uint8)) * rng.uniform(0.1, 0.3)
        ink_e = np.clip(np.maximum(ink_e, bleed), 0, 1)
    ink_e = cv2.GaussianBlur(ink_e, (0, 0), rng.uniform(0.6, 1.4))          # soft edges
    # MATCH the crop's existing ink darkness: overlay ink must be as dark as the real
    # field lines already in this bg (else the synthetic glyphs read too light).
    ink_tone = float(np.percentile(bg, 2))                                   # ~darkest existing ink
    dark = min(dark, ink_tone + rng.uniform(0, 18))
    darkf = dark * (0.7 + 0.6 * _lowfreq((h, w), rng, rng.integers(8, 20)))  # ink tone varies
    img = bg * (1 - ink_e) + darkf * ink_e
    # 4. foxing / paper blemishes: scattered soft dark spots on paper
    for _ in range(rng.integers(0, 8)):
        c = (int(rng.integers(0, w)), int(rng.integers(0, h))); r = int(rng.integers(2, 7))
        spot = np.zeros((h, w), np.float32); cv2.circle(spot, c, r, 1.0, -1)
        spot = cv2.GaussianBlur(spot, (0, 0), rng.uniform(1, 3)) * rng.uniform(0.1, 0.35)
        img = img * (1 - spot) + rng.uniform(90, 170) * spot
    # 5. global blur (sometimes heavy) + gamma
    if rng.random() < 0.7: img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.4, 1.6))
    g = rng.uniform(0.8, 1.3); img = 255.0 * (np.clip(img, 0, 255) / 255.0) ** g
    # 6. speckle: gaussian + sparse salt & pepper
    img = img + rng.normal(0, rng.uniform(2, 7), img.shape)
    sp = rng.random(img.shape); img[sp < 0.002] = rng.uniform(0, 60); img[sp > 0.998] = rng.uniform(200, 255)
    return np.clip(img, 0, 255).astype(np.uint8)
