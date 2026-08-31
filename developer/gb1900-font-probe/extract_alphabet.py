"""Extract per-letter glyphs from the human alphabet_labels.json (from make_alphabet_ui): each letter box is
cropped from its snippet, its map-linework erased (default), and it is de-rotated to upright by the label's
local baseline tangent -> a clean, human-segmented, FACE-labelled glyph. Emits alphabet_glyphs.npz (raster +
char + face + word + angle) for training, and a QC montage (letters grouped by face) to verify boxes.

Two cleaning steps applied per glyph (both improve the face kNN in A/B tests):
  - LINE-ERASE (default ON; `--no-erase-lines` to disable): removes thin straight map lines (roads/boundaries/
    contours/railways) that cross the character box edge-to-edge, discriminated from thick letter strokes by
    width, so the stroke survives where a line crosses it (line_erase.erase_crossing_lines). `--touch` uses the
    looser "line touches the box" criterion.
  - TANGENT DE-ROTATION: each letter is uprighted by the local baseline tangent of the ordered box centres, so
    curved river/canal labels yield canonical glyphs (map-layout rotation removed, font slant retained).

    python3 extract_alphabet.py --labels labels/alphabet_labels.json      # line-erase + de-rotate on by default
"""
import argparse, io, json, base64, os, math
from collections import defaultdict, Counter
import numpy as np, cv2
from PIL import Image, ImageDraw
from line_erase import erase_crossing_lines            # erase map linework crossing the character box

def tangent_angles(letters):
    # per-letter rotation = angle of the label's local baseline tangent, from the ordered box centres.
    c = [(l["x"] + l["w"] / 2.0, l["y"] + l["h"] / 2.0) for l in letters]
    n = len(c); ang = []
    for i in range(n):
        if n == 1: ang.append(0.0); continue
        j0, j1 = max(0, i - 1), min(n - 1, i + 1)          # central difference (fwd/bwd at the ends)
        dx, dy = c[j1][0] - c[j0][0], c[j1][1] - c[j0][1]
        ang.append(math.degrees(math.atan2(dy, dx)) if (dx or dy) else 0.0)
    return ang

def derotate(crop, angle_deg):
    # upright the letter: place the tight one-letter crop on a blank canvas (no neighbour ink), rotate by -angle.
    if abs(angle_deg) < 3.0: return crop                    # negligible tilt — leave as-is
    h, w = crop.shape; diag = int(math.ceil(math.hypot(h, w))) + 4
    canvas = np.full((diag, diag), 255, np.uint8)
    oy, ox = (diag - h) // 2, (diag - w) // 2
    canvas[oy:oy + h, ox:ox + w] = crop
    M = cv2.getRotationMatrix2D((diag / 2.0, diag / 2.0), angle_deg, 1.0)   # +angle rotates baseline back to horizontal
    return cv2.warpAffine(canvas, M, (diag, diag), borderValue=255)

def norm_glyph(sub, H=44, W=36):
    ys, xs = np.where(sub > 0)
    if len(ys) < 6: return None
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    g = sub[y0:y1 + 1, x0:x1 + 1].astype(np.uint8) * 255
    scale = min(H / g.shape[0], W / g.shape[1])
    g = cv2.resize(g, (max(1, int(g.shape[1] * scale)), max(1, int(g.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((H, W), np.uint8)
    oy, ox = (H - g.shape[0]) // 2, (W - g.shape[1]) // 2
    canvas[oy:oy + g.shape[0], ox:ox + g.shape[1]] = g
    return canvas

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="labels/alphabet_labels.json")
    ap.add_argument("--out", default="labels/alphabet_glyphs.npz")
    ap.add_argument("--montage", default="admin_probe/alphabet_glyphs_qc.png")
    ap.add_argument("--erase-lines", dest="erase_lines", action="store_true", default=True,
                    help="erase map linework crossing each character box (DEFAULT ON)")
    ap.add_argument("--no-erase-lines", dest="erase_lines", action="store_false",
                    help="disable map-line erasure")
    ap.add_argument("--touch", action="store_true", help="erase lines TOUCHING the box (1 end on edge), not just spanning it")
    a = ap.parse_args()
    words = json.load(open(a.labels))
    glyphs, chars, facels, wordi, raws = [], [], [], [], []
    per_face = defaultdict(list)
    angs = []; nline = 0
    skipped_noface = 0
    for wi, w in enumerate(words):
        if not w.get("face"):                               # boxed but no face assigned -> not usable, skip
            skipped_noface += 1; continue
        img = np.asarray(Image.open(io.BytesIO(base64.b64decode(w["img"]))).convert("L"), np.uint8)
        tang = tangent_angles(w["letters"])
        for li, lt in enumerate(w["letters"]):
            x, y, ww, hh = lt["x"], lt["y"], lt["w"], lt["h"]
            crop = img[max(0, y):y + hh, max(0, x):x + ww]
            if crop.size == 0: continue
            if a.erase_lines:
                crop, ne = erase_crossing_lines(crop, require_span=not a.touch); nline += (ne > 0)
            up = derotate(crop, tang[li])                   # de-rotate by the local baseline tangent
            ink = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1] > 0
            ng = norm_glyph(ink)
            if ng is None: continue
            glyphs.append(ng); chars.append(lt["char"]); facels.append(w["face"]); wordi.append(wi); angs.append(round(tang[li], 1))
            raws.append(crop); per_face[w["face"]].append((lt["char"], up, round(tang[li], 1)))
    np.savez_compressed(a.out, glyphs=np.array(glyphs), chars=np.array(chars),
                        faces=np.array(facels), word=np.array(wordi), angle=np.array(angs))
    print(f"extracted {len(glyphs)} glyphs from {len(words)-skipped_noface} words ({skipped_noface} skipped: no face) -> {a.out}")
    if a.erase_lines: print(f"line-erase: cleaned a crossing line from {nline} glyphs")
    print("per face:", dict(Counter(facels)))
    sig = [a2 for a2 in angs if abs(a2) >= 3.0]
    print(f"de-rotated by local tangent: {len(sig)}/{len(angs)} glyphs had |tilt|>=3 deg (curved labels)"
          + (f"; range {min(sig):.0f}..{max(sig):.0f} deg" if sig else " — all labels ~horizontal"))
    # QC montage: one row per face, its letter crops
    faces = list(per_face); pad = 6; ch = 58; lw = 190
    maxn = max(len(v) for v in per_face.values()); rowh = ch + 12
    W = lw + maxn * (ch + pad) + 20; Himg = len(faces) * rowh + 10
    canvas = Image.new("RGB", (W, Himg), (255, 255, 255)); d = ImageDraw.Draw(canvas)
    for r, f in enumerate(faces):
        y = r * rowh + 6; d.text((6, y + 18), f"{f} ({len(per_face[f])})", fill=(0, 0, 0))
        for i, (c, crop, ang) in enumerate(per_face[f]):
            im = Image.fromarray(crop).convert("L"); im.thumbnail((ch, ch))
            x = lw + i * (ch + pad); canvas.paste(im, (x, y))
            d.text((x, y + ch), c + (f" {ang:+.0f}°" if abs(ang) >= 3 else ""), fill=(160, 40, 40))
    os.makedirs(os.path.dirname(a.montage), exist_ok=True); canvas.save(a.montage)
    print(f"wrote QC montage {a.montage} ({canvas.size})")

if __name__ == "__main__":
    main()
