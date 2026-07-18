"""Synthetic font-style CLASSES for the GB1900 typography feasibility probe.

Each class is a *rendering recipe* (typeface + treatment) chosen to mirror one of the
real OS six-inch label treatments the VLM confused (upright vs italic serif, slab/Egyptian,
outline caps, spaced caps, blackletter). Every class is rendered with MANY random words /
abbreviations, so the supervised-contrastive label is FONT STYLE, never the characters —
that is what forces the encoder to be content-invariant (see README).

render_ink(text, recipe, rng) -> float32 HxW ink-alpha (0=paper, 1=full ink).
Compositing onto real map backgrounds + degradation lives in degrade.py.
"""
import os, numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

FONT_DIR = os.environ.get("FONT_DIR", os.path.join(os.path.dirname(__file__), "fonts"))

def _f(name):
    return os.path.join(FONT_DIR, name)

# --- class recipes -------------------------------------------------------------
# fonts: candidate faces (one picked at random per sample)
# case:  'mixed' | 'caps' | 'any'
# fill:  'solid' | 'outline'
# track: extra inter-glyph spacing as a fraction of the font size (0 = normal)
CLASSES = {
    "serif_upright":  dict(fonts=["FreeSerif.ttf", "DejaVuSerif.ttf", "NimbusRoman-Regular.otf"], case="any",   fill="solid",   track=0.0),
    "serif_italic":   dict(fonts=["FreeSerifItalic.ttf", "DejaVuSerif-Italic.ttf", "NimbusRoman-Italic.otf"],   case="mixed", fill="solid", track=0.0),
    "slab":           dict(fonts=["RobotoSlab.ttf"],                                              case="any",   fill="solid",   track=0.0),
    "sans":           dict(fonts=["FreeSans.ttf", "NimbusSans-Regular.otf"],                      case="any",   fill="solid",   track=0.0),
    "caps_spaced":    dict(fonts=["FreeSerif.ttf", "NimbusRoman-Regular.otf", "FreeSans.ttf"],    case="caps",  fill="solid",   track=0.55),
    "caps_outline":   dict(fonts=["FreeSerif.ttf", "NimbusRoman-Regular.otf"],                    case="caps",  fill="outline", track=0.15),
    "blackletter":    dict(fonts=["PirataOne.ttf", "UnifrakturCook.ttf"],                         case="mixed", fill="solid",   track=0.0),
    "engraved_caps":  dict(fonts=["Cinzel.ttf"],                                                  case="caps",  fill="solid",   track=0.10),
}
CLASS_NAMES = list(CLASSES)
CLASS_IDX = {c: i for i, c in enumerate(CLASS_NAMES)}

# short OS abbreviations appear across styles on real sheets -> include them in every class
ABBREV = ["B.M.", "F.P.", "S.P.", "B.", "F.", "S.", "P.", "W.", "G.", "Ch.", "Sch.", "Sp.", "Mon.", "Fm.", "Ho."]
_SYL_A = ["ton","ley","ford","ham","don","by","wick","worth","field","bury","dale","mere","stead","wood","combe","borough","hall","grange","court","moor"]
_SYL_B = ["Ash","Black","Wes","Ne","Old","Long","King","Church","Stan","Bar","Hol","Pen","Aber","Whit","Har","Brad","Cot","Up","Red","Green"]

def random_text(rng, cap_hint=None):
    r = rng.random()
    if r < 0.28:
        return rng.choice(ABBREV)
    if r < 0.45:
        return rng.choice(_SYL_B)                      # single word
    return rng.choice(_SYL_B) + rng.choice(_SYL_A)     # compound placename

def _load(font_file, px):
    return ImageFont.truetype(_f(font_file), px)

def render_ink(text, recipe, rng):
    """Render `text` under `recipe` to a float ink-alpha map (H rows ~ target cap band)."""
    font_file = rng.choice(recipe["fonts"])
    case = recipe["case"]
    if case == "caps" or (case == "any" and rng.random() < 0.5):
        text = text.upper()
    px = int(rng.uniform(40, 56))
    font = _load(font_file, px)
    track = int(recipe["track"] * px)
    pad = px // 2

    # measure (per-glyph so we can apply tracking)
    dummy = Image.new("L", (8, 8), 0)
    dd = ImageDraw.Draw(dummy)
    widths = []
    for ch in text:
        bb = dd.textbbox((0, 0), ch, font=font)
        widths.append(bb[2] - bb[0] + (2 if ch == " " else 0))
    total_w = sum(widths) + track * max(0, len(text) - 1) + 2 * pad
    H = int(px * 1.7) + 2 * pad
    img = Image.new("L", (max(8, total_w), H), 0)   # 0 = no ink
    d = ImageDraw.Draw(img)
    x = pad
    ybase = pad
    for ch, w in zip(text, widths):
        d.text((x, ybase), ch, fill=255, font=font)
        x += w + track
    a = np.asarray(img, dtype=np.float32) / 255.0

    if recipe["fill"] == "outline":
        sol = a > 0.4
        er = ndi.binary_erosion(sol, iterations=max(1, px // 22))
        a = (sol & ~er).astype(np.float32)

    # crop to ink bbox with small margin
    ys, xs = np.where(a > 0.15)
    if len(xs) == 0:
        return np.zeros((16, 16), np.float32)
    m = px // 5
    y0, y1 = max(0, ys.min() - m), min(a.shape[0], ys.max() + m)
    x0, x1 = max(0, xs.min() - m), min(a.shape[1], xs.max() + m)
    return a[y0:y1, x0:x1]
