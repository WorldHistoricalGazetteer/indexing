#!/usr/bin/env python
"""Open-boundary SEARCH PRIORS for boundary extraction (NOT training labels, NOT output).

ETHICS GATE (see plan-gb1900-parish-extraction.md): only **openly-licensed** boundaries
may be used as priors — HCT / `ukhc` historic counties (county-borders.co.uk: free incl.
commercial, attribution) and OS Boundary-Line parishes (OGL). CAMPOP / GBHGIS are
safeguarded → **validation-only, never a prior/seed/training signal** (using them to shape
the output would launder restricted data into a nominally-open layer).

A prior only **reweights** the raster-traced boundary probability and **logs divergence**;
the published geometry always stays the independent raster tracing. Divergence (prior says
boundary, raster doesn't — or vice versa) is itself a signal (where boundaries changed).

SCOPE (verified 2026-07-17): HCT priors apply at the **county** level only. Sub-county
boundaries (parish/district/Union/R.D.) have NO clean open prior — Unions/RDs were
abolished (1930/1974) and modern civil parishes are a different geography — so those levels
rely on the intrinsic mereing signature (CV) + CAMPOP validation-only.
"""
import math, numpy as np, cv2


def geo_to_px(lon, lat, geo):
    n = 2**geo["z"]
    xt = (lon + 180) / 360 * n
    yt = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return (xt - geo["x0"]) * 256, (yt - geo["y0"]) * 256


def hct_county_lines(shp_path, geo, pad=0.01):
    """LineStrings of HCT county borders passing through the stitch region (OPEN data)."""
    import shapefile
    from shapely.geometry import shape, box
    region = box(geo["lon_left"]-pad, geo["lat_bot"]-pad, geo["lon_right"]+pad, geo["lat_top"]+pad)
    r = shapefile.Reader(shp_path); lines = []
    for sr in r.shapeRecords():
        geom = shape(sr.shape.__geo_interface__)
        if not geom.intersects(region):
            continue
        clip = geom.boundary.intersection(region)
        for ls in ([clip] if clip.geom_type == "LineString" else getattr(clip, "geoms", [])):
            if ls.geom_type == "LineString" and ls.length > 0:
                lines.append(list(ls.coords))
    return lines


def rasterize_prior(lines, geo, hw, dilate_px=25, blur=15):
    """Soft prior corridor [0,1] from open boundary lines (dilated + blurred)."""
    H, W = hw; m = np.zeros((H, W), np.uint8)
    for coords in lines:
        pts = np.array([geo_to_px(x, y, geo) for x, y in coords], np.int32)
        cv2.polylines(m, [pts], False, 255, 1)
    if not m.any():
        return np.zeros((H, W), np.float32)
    m = cv2.dilate(m, np.ones((2*dilate_px+1, 2*dilate_px+1), np.uint8))
    return cv2.GaussianBlur(m.astype(np.float32)/255.0, (0, 0), blur)


def apply_prior(prob, prior, weight=0.5):
    """Reweight raster boundary prob by the prior (does NOT replace it). prior in [0,1].
    weight=0 → ignore prior; 1 → full multiplicative gate. Returns reweighted prob."""
    if prior is None or not prior.any():
        return prob
    return prob * ((1 - weight) + weight * np.clip(prior, 0, 1))


def divergence(prob, prior, thr=0.4):
    """Log where raster prediction and prior disagree (for QA, not for output)."""
    pb = prob > thr; pr = prior > 0.3
    return dict(pred_no_prior=int((pb & ~pr).sum()),      # raster boundary where prior has none
                prior_no_pred=int((~pb & pr).sum()),      # prior corridor with no raster boundary
                agree=int((pb & pr).sum()))
