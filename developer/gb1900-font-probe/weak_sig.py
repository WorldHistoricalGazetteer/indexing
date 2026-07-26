"""Weak typographic labels from the GB1900 transcript — a SAMPLING PRIOR, never ground truth.

The OS Characteristic Sheet assigns a typeface by feature type, so a transcript that says "Tumulus" or "River"
implies a face, and through `font_taxonomy.json` a SIGNATURE. That makes thousands of free weak labels, which
is what turns Phase C's bottleneck from "label hundreds by eye" into "confirm a lexicon-seeded sample".

**The circularity trap, stated once so nobody walks into it:** the thing under test is whether TYPOGRAPHY
carries feature type. A weak label derived from the WORD CONTENT is therefore not evidence about the font — if
it were used as ground truth, the measurement would be scoring the lexicon against itself. Weak labels are
allowed to do exactly two things: choose which crops a human is shown, and pre-fill the answer they correct.
Every reported number must come from human-verified labels only.

The lexicon is deliberately the same one used elsewhere (`make_alphabet_ui.LEX`) — one vocabulary, not a fork.
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
_TAX_PATHS = [f"{HERE}/font_taxonomy.json", "/vast/ishi/gb1900/probe/font/font_taxonomy.json"]


def _load_face_sig():
    for p in _TAX_PATHS:
        if os.path.exists(p):
            tax = json.load(open(p))
            return {f["key"]: "·".join(str(f.get(x)) for x in ("base_style", "fill", "decor")) for f in tax}
    raise FileNotFoundError(f"font_taxonomy.json not found in {_TAX_PATHS}")


FACE_SIG = _load_face_sig()

# Ordered: FIRST MATCH WINS, so unambiguous head-nouns must precede ambiguous ones. The antiquity list contains
# common words ("Stone", "Cross", "Camp") that also appear as modifiers in ordinary names — "Stone Works" is a
# manufactory, not a tumulus — so the reliable feature nouns are tested first. Precision matters more than
# recall here: a human corrects every label anyway, but a systematically mis-seeded class wastes their time.
LEX = [
    ("Navigable Rivers and Canals", r"\b(Canal|Navigation)\b"),
    ("small_rivers", r"\b(River|Brook|Burn|Beck|Nant|Afon|Stream|Well|Pool|Mere|Lake|Ford|Water|Dyke)\b"),
    ("manufactories", r"\b(Mill|Works|Factory|Colliery|Foundry|Quarry|Pit)\b"),
    ("other_stations", r"\b(Station|Sta\.|Junction)\b"),
    ("parish_churches", r"\b(Church|Chapel|Ch\b)\b"),
    ("gentlemens_seats", r"\b(Hall|Lodge|House|Grange|Court|Manor)\b"),
    ("woods_copses", r"\b(Wood|Copse|Plantation|Covert|Spinney)\b"),
    ("antiq_roman", r"\bRoman\b"),
    # NB `Tumul` is a PREFIX (Tumulus/Tumuli) — make_alphabet_ui.LEX writes it inside a trailing \b, which can
    # never match, so antiquities were silently under-recalled there. Anchored with \w* here.
    ("antiq_saxon", r"\b(Tumul\w*|Cairn|Barrow|Earthwork|Camp|Stone|Cross|Site of)\b"),
    ("antiq_subsequent", r"\b(Castle|Priory|Abbey|Moat|Tower)\b"),
    ("Bogs, Moors and Forests", r"\b(Bog|Moor|Common|Marsh|Fen|Heath|Forest)\b"),
    ("ranges_hills", r"\b(Hill|Down|Fell|Tor|Ridge|Beacon)\b"),
]
COMPILED = [(face, re.compile(pat, re.I)) for face, pat in LEX]

# Spot heights, bench-mark values and contour numbers are set in the numeral face, which has no entry in the
# inventory. Left unfiltered they are not merely clutter: with the numeral anchors dropped, each one is
# proposed as whichever LETTERING face sits nearest — a confident wrong answer rather than an abstention.
_ALPHA_TOK = re.compile(r"[A-Za-z]+\.?")


def is_numeral(text):
    """Two or more digits and no WORDS. Abbreviations do not defeat it.

    A whole-string numeric test was too strict: it rejected "B.M. 155·9" and "F.P. 47·7", which are numeral-face
    labels carrying an abbreviation, while a bare "29" and a spot height are exactly what should be caught. So
    the test is on the two things that actually distinguish the numeral face — it carries digits, and it carries
    no running words. An alphabetic run counts as a word only if it is three or more letters AND not
    abbreviated with a full stop, which lets B.M., F.P., No. and Ho. through while stopping STREET and Cricket.
    """
    t = text or ""
    if sum(c.isdigit() for c in t) < 2:
        return False
    for tok in _ALPHA_TOK.findall(t):
        if len(tok.rstrip(".")) >= 3 and not tok.endswith("."):
            return False
    return True


def weak_sig(text):
    """(signature, rule) implied by the transcript, or (None, None) if it implies nothing.

    Returning None is the common and correct outcome — most labels are bare place names with no type word.
    Do not invent a fallback signature for them; an unlabelled candidate is honest, a guessed one is noise.
    """
    t = (text or "").strip()
    if not t:
        return None, None
    if is_numeral(t):                      # one definition, shared with the review-page filters
        return "numeral·solid·plain", "numeral"
    for face, rx in COMPILED:
        if rx.search(t):
            sig = FACE_SIG.get(face)
            if sig:
                return sig, f"lex:{face}"
    return None, None
