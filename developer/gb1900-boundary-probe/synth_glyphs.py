#!/usr/bin/env python
"""Consolidated synthetic mereing-glyph renderer (CORRECTED geometry).

Corrections from grounding on the real z17 raster (user-verified):
- x scale: ~6-8px half-arm (was 9-14, too big).
- x ORIENTATION: the base of the x is parallel to the LOCAL boundary tangent, i.e.
  the x rotates with the line (NOT fixed-upright). Arms at +/-45deg to the tangent.
- mereing ARROWS: tangent-oriented (point along the local boundary), a boundary-only cue.
Multi-class label map: 0 bg, 1 dot, 2 dash, 3 cross, 4 arrow, 5 solid.
"""
import numpy as np, cv2

CLASSES = ["bg", "dot", "dash", "cross", "arrow", "solid"]


def tangent_normal(p0, p1):
    t = (p1 - p0).astype(np.float32); n = np.linalg.norm(t)
    t = t / n if n > 1e-6 else np.array([1.0, 0.0], np.float32)
    return t, np.array([-t[1], t[0]], np.float32)


def draw_cross(ink, comp, c, t, n, a, th, rng):
    """x with base PARALLEL to tangent t: arms along (t+n) and (t-n)."""
    aj = np.deg2rad(rng.uniform(-8, 8))                     # slight engraving jitter
    ca, sa = np.cos(aj), np.sin(aj)
    tr = np.array([ca*t[0]-sa*t[1], sa*t[0]+ca*t[1]]); nr = np.array([-tr[1], tr[0]])
    d1 = (tr + nr); d2 = (tr - nr)
    for d in (d1, d2):
        p = (c + a*d).astype(int); q = (c - a*d).astype(int)
        cv2.line(ink, tuple(q), tuple(p), 1.0, th); cv2.line(comp, tuple(q), tuple(p), 3, th)


def draw_arrow(ink, comp, c, t, n, a, rng):
    """chevron/arrow pointing along +t (mereing 'points to the mere side')."""
    tip = (c + a*t).astype(int)
    for s in (+1, -1):
        w = (c - a*0.6*t + s*a*0.7*n).astype(int)
        cv2.line(ink, tuple(w), tuple(tip), 1.0, 2); cv2.line(comp, tuple(w), tuple(tip), 4, 2)


def smooth_path(P, rng, n=6):
    pts = rng.integers(6, P-6, size=(n, 2)).astype(np.float32); pts = pts[np.argsort(pts[:, 0])]
    ts = np.linspace(0, 1, 300)
    xs = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 0]), np.ones(11)/11, "same")
    ys = np.convolve(np.interp(ts, np.linspace(0, 1, n), pts[:, 1]), np.ones(11)/11, "same")
    return np.stack([xs, ys], 1)


def render_boundary(P, path, rng, ink=None, comp=None):
    """Render a mereing boundary (dots + tangent-aligned x's + arrows) onto ink/comp layers.

    x's and arrows sit OFFSET to one side of the line (the mere side), along the local
    normal — matching the real sheets (they are not drawn on the line itself).
    """
    if ink is None: ink = np.zeros((P, P), np.float32)
    if comp is None: comp = np.zeros((P, P), np.uint8)
    # dots DOMINATE: big, round, regularly spaced. x's + arrows are RARE punctuation.
    d_pitch = rng.uniform(18, 26); dr = int(rng.choice([2, 3]))          # regular pitch, consistent size
    x_pitch = rng.uniform(180, 340); ar_pitch = rng.uniform(180, 340)
    side = rng.choice([-1.0, 1.0])                        # mere side, consistent along the boundary
    x_off = rng.uniform(12, 40); ar_off = rng.uniform(4, 14)   # x's often sit well off the line
    x_cap = int(rng.choice([0, 1, 1, 1, 2])); ar_cap = int(rng.choice([0, 0, 1, 1]))  # rare per tile
    nx = na = 0
    acc = 0.0; xa = rng.uniform(0, 200); ara = rng.uniform(0, 200)
    for i in range(1, len(path)):
        p0, p1 = path[i-1], path[i]; t, n = tangent_normal(p0, p1)
        seg = np.hypot(*(p1-p0)); acc += seg; xa += seg; ara += seg
        if acc >= d_pitch:
            acc = 0; c = p1.astype(int)
            cv2.circle(ink, tuple(c), dr, 1.0, -1); cv2.circle(comp, tuple(c), dr, 1, -1)
        if xa >= x_pitch and nx < x_cap:
            xa = 0; x_pitch = rng.uniform(180, 340); nx += 1
            c = p1 + side * x_off * rng.uniform(0.7, 1.3) * n + rng.uniform(-2, 2, 2)   # mere-side offset
            draw_cross(ink, comp, c, t, n, int(rng.integers(6, 9)), int(rng.integers(2, 4)), rng)
        if ara >= ar_pitch and na < ar_cap:
            ara = 0; ar_pitch = rng.uniform(180, 340); na += 1
            draw_arrow(ink, comp, p1 + side * ar_off * n, t, n, int(rng.integers(6, 9)), rng)
    return ink, comp


def render_footpath(P, path, rng, ink, comp):
    """Footpath. Normally DOUBLE parallel dashed ('double-pecked'). BUT when it runs
    parallel to a solid-lined feature (wall/road), it is delimited by a SINGLE dashed
    rail on the open side + the solid line on the other. A key NEGATIVE (not the dotted
    boundary). comp: dash=2, solid=5.  Calibrated to a real Bakewell F.P. (z17)."""
    gap = rng.uniform(2.5, 4.0)                            # half-separation of the rails
    dash_len = rng.uniform(4, 8); dash_pitch = rng.uniform(9, 14)
    alongside = rng.random() < 0.4
    if alongside:
        solid_side = rng.choice([-1.0, 1.0]); rails = [-solid_side]   # solid one side, dashes other
        th = int(rng.integers(1, 3))
        for i in range(1, len(path)):
            p0, p1 = path[i-1], path[i]; _, n = tangent_normal(p0, p1)
            a = p0 + solid_side*gap*n; b = p1 + solid_side*gap*n
            cv2.line(ink, tuple(a.astype(int)), tuple(b.astype(int)), 1.0, th)
            cv2.line(comp, tuple(a.astype(int)), tuple(b.astype(int)), 5, th)
    else:
        rails = [-1.0, 1.0]
    for s in rails:
        acc = 0.0 if s < 0 else dash_pitch/2               # stagger the two rails
        for i in range(1, len(path)):
            p0, p1 = path[i-1], path[i]; t, n = tangent_normal(p0, p1)
            acc += np.hypot(*(p1-p0))
            if acc >= dash_pitch:
                acc = 0
                a = p1 + s*gap*n - t*dash_len/2; b = p1 + s*gap*n + t*dash_len/2
                cv2.line(ink, tuple(a.astype(int)), tuple(b.astype(int)), 1.0, 2)
                cv2.line(comp, tuple(a.astype(int)), tuple(b.astype(int)), 2, 2)
    return ink, comp
