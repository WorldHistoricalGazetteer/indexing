#!/bin/bash
#SBATCH -J tslant
#SBATCH --account=ishi
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH -t 00:10:00
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/tslant.log
cd /vast/ishi/gb1900/probe/font
/vast/ishi/envs/boundary/bin/python - <<'PY'
import sys; sys.path.insert(0,"/vast/ishi/gb1900/probe/font")
import numpy as np, cv2, math, json, glob
from discrim_test import norm_glyph
from build_alphabet_multi import glyph_slant, label_slant, gpoly_cap_h
from make_font_testset_v2 import derotate
from build_alphabet import force_split

# 1) SYNTHETIC: a vertical bar 'I' sheared by known angles; does norm_glyph preserve the lean?
def sheared_letter(deg):
    img = np.zeros((60,60), np.uint8)
    cv2.line(img,(30,8),(30,52),255,4)                 # vertical stroke
    cv2.line(img,(20,8),(40,8),255,3); cv2.line(img,(20,52),(40,52),255,3)  # serifs -> looks like 'I'
    t = math.tan(math.radians(deg))
    M = np.float32([[1,t,-t*30],[0,1,0]])
    return cv2.warpAffine(img, M, (60,60))
print("SYNTHETIC vertical-stroke sheared by known angle:")
for deg in (0,10,20,28):
    raw = sheared_letter(deg); ng = norm_glyph(raw>0)
    sr = glyph_slant(raw>0); sn = glyph_slant(ng) if ng is not None else None
    dr = math.degrees(math.atan(sr)) if sr is not None else None
    dn = math.degrees(math.atan(sn)) if sn is not None else None
    print(f"  shear={deg:2}deg  raw_slant~{dr:+.1f}deg   norm_glyph_slant~{dn if dn is None else round(dn,1)}deg")

# 2) REAL: measure slant on raw derotated ink vs the norm_glyph'd glyphs, for a few spotted labels
print("\nREAL spotted labels (raw-ink slant vs normed-glyph slant):")
n=0
for f in sorted(glob.glob("/vast/ishi/gb1900/edition/spot/boxes_gb_*.jsonl")):
    for line in open(f):
        try: r=json.loads(line)
        except: continue
        if r.get("score",0)<0.6 or not r.get("gpoly"): continue
        t=r["text"]; al=[c for c in t if c.isalnum()]
        if len(al)<4: continue
        patch=derotate(r)
        if patch is None: continue
        # raw-ink slant of the whole derotated word
        ink=(cv2.threshold(patch,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1])>0
        sraw=glyph_slant(ink)
        gs=force_split(patch,len(al))
        if len(gs)!=len(al): continue
        snorm=label_slant([(al[i].upper(),al[i].isupper(),gs[i]) for i in range(len(al))])
        dr = round(math.degrees(math.atan(sraw)),1) if sraw is not None else None
        dn = round(math.degrees(math.atan(snorm)),1) if snorm is not None else None
        print(f"  {t[:20]:22} raw_ink~{dr}deg  normed~{dn}deg")
        n+=1
        if n>=12: break
    if n>=12: break
PY
echo DONE
