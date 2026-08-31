"""GB-STAMP — detect the human-annotated FILL attribute (solid | none[outline] | diagonal | horizontal hatch)
from glyph pixels, and validate against cs_decisions on the reference exemplars. Fill is the strongest of the
annotated discriminators (hatch = strong periodic texture, outline = low fill-ratio) and, unlike cap-height /
slant, should survive in real map lettering. If it classifies the clean exemplars well, it extends to the
harvest crops to fix face assignment.

    python3 fill_decor_probe.py
"""
import glob, json, os
import numpy as np, cv2

HERE = os.path.dirname(os.path.abspath(__file__))
ann = {(e["key"][3:] if e["key"].startswith("ex_") else e["key"]): e for e in json.load(open(f"{HERE}/labels/cs_decisions (5).json"))}
tax = json.load(open(f"{HERE}/font_taxonomy.json"))
lbl2key = {f["label"]: f["key"] for f in tax}

def ink_mask(gray):
    _, m = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return m

def fill_holes(m):
    ff = m.copy(); h, w = m.shape
    cv2.floodFill(ff, np.zeros((h + 2, w + 2), np.uint8), (0, 0), 255)   # flood background from corner
    return m | cv2.bitwise_not(ff)                                        # add back enclosed holes

def hatch_peak(m):
    # 2D FFT of the ink mask; parallel hatch lines -> strong off-DC peak. Return (strength, angle_deg).
    g = (m > 0).astype(np.float32); g -= g.mean()
    if g.std() < 1e-6: return 0.0, 0.0
    F = np.abs(np.fft.fftshift(np.fft.fft2(g * np.hanning(g.shape[0])[:, None] * np.hanning(g.shape[1])[None, :])))
    cy, cx = np.array(F.shape) // 2
    F[cy - 2:cy + 3, cx - 2:cx + 3] = 0                                   # kill DC + very-low freq
    peak = np.unravel_index(np.argmax(F), F.shape)
    r = np.hypot(peak[0] - cy, peak[1] - cx)
    strength = F[peak] / (F.mean() + 1e-6)
    ang = np.degrees(np.arctan2(peak[0] - cy, peak[1] - cx)) % 180
    return strength / max(1.0, r ** 0.0), ang

def classify_fill(gray):
    m = ink_mask(gray)
    filled = fill_holes(m)
    fr = m.sum() / (filled.sum() + 1e-6)
    strength, ang = hatch_peak(m)
    # hatch lines create a periodic peak; its angle is PERPENDICULAR to the line direction.
    if strength >= 14 and fr < 0.72:
        # line direction = ang+90; horizontal lines -> line dir ~0/180 ; diagonal -> ~45/135
        linedir = (ang + 90) % 180
        return "horizontal" if (linedir < 25 or linedir > 155) else "diagonal", fr, strength
    if fr >= 0.6:
        return "solid", fr, strength
    return "none", fr, strength

def main():
    rows = []; correct = 0; n = 0
    for p in sorted(glob.glob(f"{HERE}/reference/exemplars/*.jpg")):
        key = os.path.basename(p)[:-4]
        a = ann.get(key) or ann.get(lbl2key.get(key, ""), {})
        gt = a.get("fill")
        gray = np.asarray(cv2.imread(p, cv2.IMREAD_GRAYSCALE))
        if gray is None: continue
        pred, fr, st = classify_fill(gray)
        ok = (pred == gt)
        if gt: n += 1; correct += ok
        rows.append((ok, key, gt, pred, fr, st))
    rows.sort(key=lambda r: (r[2] or "", not r[0]))
    print(f"FILL detection on exemplars: {correct}/{n} = {correct/max(1,n):.0%}\n")
    print(f"{'ok':3} {'face':24} {'annotated':11} {'predicted':11} fill_ratio hatch_str")
    for ok, key, gt, pred, fr, st in rows:
        print(f"{'✓' if ok else '✗':3} {key:24} {str(gt):11} {pred:11} {fr:.2f}      {st:.1f}")
    # confusion by annotated class
    from collections import defaultdict
    conf = defaultdict(lambda: defaultdict(int))
    for ok, key, gt, pred, fr, st in rows:
        if gt: conf[gt][pred] += 1
    print("\nconfusion (annotated -> predicted counts):")
    for gt in sorted(conf): print(f"  {gt:11} -> {dict(conf[gt])}")

if __name__ == "__main__":
    main()
