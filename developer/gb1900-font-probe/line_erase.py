"""Erase map linework that crosses a MapReader character box, to clean the glyph for font recognition. Roads,
boundaries, contours, railways are THIN STRAIGHT lines that cross the letter box edge-to-edge; the letter's own
strokes are THICK and self-contained. So: isolate thin structures (ink minus a stroke-width morphological
opening), Hough-detect long straight segments among those that SPAN the box (both ends on the boundary), and
erase only the thin pixels along them — thick letter strokes survive even where a line crosses them.

Test: python3 line_erase.py --labels "labels/alphabet_labels (2).json"  -> admin_probe/line_erase_qc.png
"""
import argparse, io, json, base64
import numpy as np, cv2
from PIL import Image, ImageDraw

def erase_crossing_lines(gray, require_span=True):
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]   # letter=255
    H, W = ink.shape
    if int(ink.sum()) == 0: return gray, 0
    dt = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    sw = max(1.0, float(np.median(dt[dt > 0.5])) if (dt > 0.5).any() else 1.0)       # letter half stroke-width
    # Detect straight segments on the FULL ink (map line is continuous there; the thin-layer subtract fragmented it).
    lines = cv2.HoughLinesP(ink, 1, np.pi / 180, threshold=max(10, int(0.4 * min(H, W))),
                            minLineLength=int(0.55 * max(H, W)), maxLineGap=5)
    if lines is None: return gray, 0
    diag = np.hypot(H, W); mask = np.zeros_like(ink); n = 0
    edge = lambda x, y, m=3: x <= m or x >= W - 1 - m or y <= m or y >= H - 1 - m
    for x1, y1, x2, y2 in lines[:, 0, :]:
        L = np.hypot(x2 - x1, y2 - y1)
        if L < 0.55 * diag: continue
        if require_span and not (edge(x1, y1) and edge(x2, y2)): continue            # spans the box edge-to-edge
        # discriminate map-line (THIN) from letter-stroke (THICK): median distance-transform ALONG the segment.
        m = max(8, int(L)); xs = np.clip(np.linspace(x1, x2, m).astype(int), 0, W - 1)
        ys = np.clip(np.linspace(y1, y2, m).astype(int), 0, H - 1); vals = dt[ys, xs]; on = vals > 0.3
        if on.sum() < 0.6 * m: continue                                             # must lie on ink most of its length
        if np.median(vals[on]) > sw * 0.8: continue                                 # THICK -> letter stroke, keep it
        cv2.line(mask, (x1, y1), (x2, y2), 255, thickness=max(1, int(round(sw)) + 1)); n += 1
    erase = (mask > 0) & (dt <= sw * 1.3)                                            # erase thin pixels only (protect strokes)
    out = gray.copy(); out[erase] = 255
    return out, n

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--labels", default="labels/alphabet_labels (2).json")
    ap.add_argument("--out", default="admin_probe/line_erase_qc.png"); a = ap.parse_args()
    words = json.load(open(a.labels)); shown = []
    for w in words:
        img = np.asarray(Image.open(io.BytesIO(base64.b64decode(w["img"]))).convert("L"), np.uint8)
        for lt in w["letters"]:
            x, y, ww, hh = lt["x"], lt["y"], lt["w"], lt["h"]
            crop = img[max(0, y):y + hh, max(0, x):x + ww]
            if crop.size == 0: continue
            cleaned, n = erase_crossing_lines(crop)
            if n > 0: shown.append((lt["char"], w["face"], crop, cleaned, n))     # only show glyphs a line was erased from
    shown = shown[:40]
    print(f"glyphs with a crossing line erased: {len(shown)} (showing up to 40)")
    if not shown: return
    ch = 64; cols = 4; rows = (len(shown) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * (2 * ch + 60), rows * (ch + 16)), (255, 255, 255)); d = ImageDraw.Draw(canvas)
    for i, (c, face, before, after, n) in enumerate(shown):
        r, cc = divmod(i, cols); x = cc * (2 * ch + 60); y = r * (ch + 16)
        for j, im0 in enumerate((before, after)):
            im = Image.fromarray(im0).convert("L"); im.thumbnail((ch, ch))
            canvas.paste(im, (x + j * (ch + 8), y))
        d.text((x, y + ch), f"{c} {face[:10]}", fill=(120, 40, 40))
    canvas.save(a.out); print(f"wrote {a.out} (left=before, right=after)")

if __name__ == "__main__":
    main()
