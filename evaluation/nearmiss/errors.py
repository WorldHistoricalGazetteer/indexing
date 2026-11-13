"""A realistic error model for place names, and the pure helpers around it. No I/O.

Genealogy place strings are mis-spelt in recognisable ways: a transcriber hears a name and writes it
phonetically, an immigrant's clerk anglicises it, OCR confuses letter shapes, a typist slips. The model
below draws one or two errors from those classes (weights are a judgement, stated here so they can be
argued with) and TAGS each query with the classes applied, so results can be reported per class.

Every generator is deterministic given a ``random.Random``.
"""
from __future__ import annotations

import random
import re
import unicodedata

# error class -> weight. Phonetic and spelling-convention errors dominate historical transcription.
CLASS_WEIGHTS = {
    "phonetic": 30,      # ph/f, ck/k, ie/y, th/t, sch/sh, tz/z, w/v ...
    "vowel": 14,         # a/e, o/u, i/e: the unstressed-vowel drift
    "double": 12,        # ll <-> l, tt <-> t
    "diacritic": 8,      # Köln -> Koln, Besançon -> Besancon
    "dropped": 10,       # a missing letter (often silent h, trailing e)
    "extra": 4,          # an inserted letter
    "transpose": 6,      # adjacent letters swapped
    "ocr": 6,            # rn <-> m, cl <-> d, u <-> n
    "typo": 6,           # keyboard-neighbour slip
    "abbrev": 4,         # Saint/St, Mount/Mt (multi-word names only)
}

PHONETIC = [("ph", "f"), ("f", "ph"), ("ck", "k"), ("k", "ck"), ("c", "k"), ("k", "c"), ("ie", "y"), ("y", "ie"),
            ("th", "t"), ("t", "th"), ("sch", "sh"), ("sh", "sch"), ("tz", "z"), ("z", "tz"), ("w", "v"),
            ("v", "w"), ("j", "y"), ("y", "j"), ("oo", "u"), ("ou", "ow"), ("ow", "ou"), ("ei", "ie"),
            ("ie", "ei"), ("ae", "e"), ("oe", "o"), ("x", "ks"), ("qu", "kw"), ("gh", "g"), ("kn", "n"),
            ("wr", "r"), ("s", "z"), ("z", "s"), ("ch", "tch"), ("ch", "sh"), ("g", "j"), ("dg", "j")]
VOWEL = {"a": "e", "e": "a", "o": "u", "u": "o", "i": "e", "e": "i", "y": "i", "i": "y"}
OCR = [("rn", "m"), ("m", "rn"), ("cl", "d"), ("d", "cl"), ("u", "n"), ("n", "u"), ("e", "c"), ("c", "e"),
       ("l", "i"), ("i", "l"), ("h", "b"), ("vv", "w")]
ABBREV = [("saint ", "st "), ("st ", "saint "), ("st. ", "saint "), ("mount ", "mt "), ("mt ", "mount "),
          ("fort ", "ft "), ("port ", "pt ")]
_KEYS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
_NEIGH: dict[str, str] = {}
for _r, _row in enumerate(_KEYS):
    for _c, _ch in enumerate(_row):
        n = ""
        if _c > 0: n += _row[_c - 1]
        if _c < len(_row) - 1: n += _row[_c + 1]
        for dr in (-1, 1):
            rr = _r + dr
            if 0 <= rr < len(_KEYS) and _c < len(_KEYS[rr]):
                n += _KEYS[rr][_c]
        _NEIGH[_ch] = n


def strip_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def fold(text: str) -> str:
    """Lower-case, diacritic-free, single-spaced comparison key."""
    return re.sub(r"\s+", " ", strip_diacritics(text).casefold()).strip()


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment), case-insensitive."""
    a, b = a.casefold(), b.casefold()
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1): d[i][0] = i
    for j in range(len(b) + 1): d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _sub_once(s: str, pairs, rng: random.Random) -> str | None:
    """Apply one randomly chosen applicable (old -> new) substitution at a random occurrence."""
    low = s.lower()
    options = []
    for old, new in pairs:
        start = 0
        while (i := low.find(old, start)) != -1:
            options.append((i, old, new))
            start = i + 1
    if not options:
        return None
    i, old, new = rng.choice(options)
    seg = s[i:i + len(old)]
    new = new.capitalize() if seg[:1].isupper() else new
    return s[:i] + new + s[i + len(old):]


def apply_class(name: str, cls: str, rng: random.Random) -> str | None:
    """One error of class ``cls``, or None when the class does not apply to this name."""
    if cls == "phonetic":
        return _sub_once(name, PHONETIC, rng)
    if cls == "vowel":
        idx = [i for i, ch in enumerate(name) if ch.lower() in VOWEL and i > 0]
        if not idx:
            return None
        i = rng.choice(idx)
        new = VOWEL[name[i].lower()]
        return name[:i] + (new.upper() if name[i].isupper() else new) + name[i + 1:]
    if cls == "double":
        dbl = [i for i in range(len(name) - 1) if name[i].lower() == name[i + 1].lower() and name[i].isalpha()]
        if dbl:
            i = rng.choice(dbl)
            return name[:i] + name[i + 1:]
        sgl = [i for i, ch in enumerate(name) if i > 0 and ch.isalpha() and ch.lower() not in "aeiouy"]
        if not sgl:
            return None
        i = rng.choice(sgl)
        return name[:i] + name[i] + name[i:]
    if cls == "diacritic":
        out = strip_diacritics(name).replace("ß", "ss")
        return out if out != name else None
    if cls == "dropped":
        silent = [i for i, ch in enumerate(name) if (ch.lower() == "h" and i > 0) or (i == len(name) - 1 and ch.lower() == "e")]
        pool = silent or [i for i in range(1, len(name)) if name[i].isalpha()]
        if not pool or len(name) < 5:
            return None
        i = rng.choice(pool)
        return name[:i] + name[i + 1:]
    if cls == "extra":
        i = rng.randrange(1, len(name))
        return name[:i] + rng.choice("aeiouhnrst") + name[i:]
    if cls == "transpose":
        idx = [i for i in range(1, len(name) - 1) if name[i].isalpha() and name[i + 1].isalpha() and name[i].lower() != name[i + 1].lower()]
        if not idx:
            return None
        i = rng.choice(idx)
        return name[:i] + name[i + 1] + name[i] + name[i + 2:]
    if cls == "ocr":
        return _sub_once(name, OCR, rng)
    if cls == "typo":
        idx = [i for i in range(1, len(name)) if name[i].lower() in _NEIGH]
        if not idx:
            return None
        i = rng.choice(idx)
        new = rng.choice(_NEIGH[name[i].lower()])
        return name[:i] + (new.upper() if name[i].isupper() else new) + name[i + 1:]
    if cls == "abbrev":
        low = name.lower()
        opts = [(o, n) for o, n in ABBREV if low.startswith(o)]
        if not opts:
            return None
        o, n = rng.choice(opts)
        return n.capitalize() + name[len(o):]
    raise ValueError(cls)


def corrupt(name: str, rng: random.Random, max_errors: int = 3, force: str | None = None) -> tuple[str, list[str]] | None:
    """Return (corrupted name, [classes applied]) or None if nothing applicable produced a change.

    One error for short names (<= 6 letters), one or two for longer, up to three for very long ones. The result always differs from the
    input after folding, so a diacritic-only change still counts (the fold used for matching may erase
    it: that is reported by the evaluator as ``folds_to_original``).
    """
    if len(name) <= 6:
        n_err = 1
    elif len(name) >= 12:
        n_err = rng.choice([1, 2, 2, 3][:max(1, max_errors + 1)])      # long names are mangled more
    else:
        n_err = rng.choice([1, 1, 2][:max_errors + 1])
    classes = list(CLASS_WEIGHTS)
    weights = [CLASS_WEIGHTS[c] for c in classes]
    cur, applied = name, []
    if force:                       # a targeted item: this class first (None if it cannot apply)
        first = apply_class(name, force, rng)
        if not first or first == name:
            return None
        cur, applied = first, [force]
    tries = 0
    while len(applied) < n_err and tries < 25:
        tries += 1
        cls = rng.choices(classes, weights)[0]
        if cls in applied:
            continue
        new = apply_class(cur, cls, rng)
        if new and new != cur and len(new) >= 3:
            cur, applied = new, applied + [cls]
    if not applied or cur == name:
        return None
    return cur, applied


def nonsense(rng: random.Random, lengths=(5, 11)) -> str:
    """A pronounceable string that is not a place name: consonant-vowel syllables."""
    cons, vow = "bdfgklmnprstvz", "aeiou"
    target = rng.randint(*lengths)
    s = ""
    while len(s) < target:
        s += rng.choice(cons) + rng.choice(vow) + (rng.choice(cons) if rng.random() < 0.35 else "")
    return s[:target].capitalize()
