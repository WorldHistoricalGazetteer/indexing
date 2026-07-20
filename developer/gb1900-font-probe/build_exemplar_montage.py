"""Lay out the 44 Characteristic-Sheet exemplar crops in a labelled grid so the seed letters can be curated
in one pass (which glyph(s) each exemplar actually shows -> clean seeds for build_alphabet_multi)."""
import json, os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
tax = [x for x in json.load(open(f"{HERE}/font_taxonomy.json")) if x.get("exemplar")]
COLS, CW, CH, LBL = 6, 170, 150, 46
rows = (len(tax) + COLS - 1) // COLS
W, H = COLS * CW, rows * (CH + LBL)
canvas = Image.new("RGB", (W, H), (255, 255, 255)); d = ImageDraw.Draw(canvas)
try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
except Exception: font = ImageFont.load_default()

for i, x in enumerate(tax):
    r, c = divmod(i, COLS); x0, y0 = c * CW, r * (CH + LBL)
    d.rectangle([x0, y0, x0 + CW - 1, y0 + CH + LBL - 1], outline=(210, 205, 195))
    try:
        im = Image.open(f"{HERE}/{x['exemplar']}").convert("L")
        im.thumbnail((CW - 14, CH - 14))
        canvas.paste(im.convert("RGB"), (x0 + (CW - im.width) // 2, y0 + (CH - im.height) // 2))
    except Exception: pass
    lab = f"{x['id']} {x['key']}"
    d.text((x0 + 5, y0 + CH + 3), lab[:26], fill=(20, 20, 20), font=font)
    d.text((x0 + 5, y0 + CH + 18), f"{x['base_style']}{'CAPS' if x['caps'] else ''}", fill=(150, 60, 40), font=font)
    d.text((x0 + 5, y0 + CH + 32), (x.get('external') or ''), fill=(80, 110, 150), font=font)

out = f"{HERE}/reference/exemplar_montage.png"
canvas.save(out)
print("wrote", out, canvas.size, "with", len(tax), "exemplars")
