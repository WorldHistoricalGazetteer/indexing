"""Paper-tone / illumination flat-field correction (iteration-2 lever a).

Estimate the smooth PAPER field (light, low-frequency; ink is a dark high-freq minority) and
divide it out, so paper -> ~1.0 and only the ink residual survives. Applied IDENTICALLY to
synthetic and real snippets so the two domains share a canonical paper -> the encoder can only
key on ink/glyph shape. Estimated at TILE scale (robust: a tile has plenty of paper pixels),
not per tiny snippet. Preserves ink weight (no binarisation).

In production these params (paper level + gradient) live per-tile in the DuckDB `tiles` table;
here we compute the field directly on the tile canvas.
"""
import numpy as np
from scipy.ndimage import maximum_filter, gaussian_filter

def paper_field(g, k=41, smooth=25):
    """g: float 0..1 (1=paper). Returns the smooth paper illumination field, same shape."""
    env = maximum_filter(g, size=k)        # fill dark ink with surrounding paper -> light envelope
    field = gaussian_filter(env, smooth)
    return np.clip(field, 0.2, 1.0)

def flatten(g, k=41, smooth=25):
    """Divide out the paper field: paper -> ~1.0, ink kept as a dark residual (weight preserved)."""
    f = paper_field(g, k, smooth)
    return np.clip(g / f, 0.0, 1.0).astype(np.float32)
