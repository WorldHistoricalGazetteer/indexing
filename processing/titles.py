# processing/titles.py
"""
The display-title rule, stated once (place#290).

Two sites choose a place's display name from its names and must agree:

* the ``wd`` extractor (``authorities/wikidata-places.py``), which stores the
  choice as ``title`` at ingest;
* the gateway's read-time repair (``gateway/titles.py``), which substitutes
  the same choice for records ingested before the extractor was fixed and
  still carrying a bare QID as their title.

If the two drifted, a re-ingest would silently change the name every
consumer shows for millions of places. ``tests/test_wd_title_prefers_label.py``
pins the parity.

The rule, over names in document order:

1. the first name tagged exactly ``en``;
2. else the first ``en-*`` variant (``en-gb``, ``en-ca``, …);
3. else the first ``mul`` ("multiple languages": Wikidata's language-neutral
   label, usually the local form);
4. else the first usable name at all.

A name is usable when it is non-empty after stripping and is not itself a
bare QID (``Q12345``). Nothing usable → ``None``; the caller decides what to
fall back to (the extractor keeps the QID, so a nameless entity is still
identifiable).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

_QID_RE = re.compile(r"^Q\d+$")


def is_bare_qid(title: Optional[str]) -> bool:
    return bool(title) and bool(_QID_RE.match(title.strip()))


def preferred_label(names: Iterable[tuple[Optional[str], Optional[str]]]) -> Optional[str]:
    """``names`` is an ordered iterable of ``(label, lang)`` pairs."""
    first_en_variant = None
    first_mul = None
    first_any = None
    for label, lang in names:
        label = (label or "").strip()
        if not label or is_bare_qid(label):
            continue
        tag = (lang or "").strip().lower()
        if tag == "en":
            return label
        if first_en_variant is None and tag.startswith("en-"):
            first_en_variant = label
        elif first_mul is None and tag == "mul":
            first_mul = label
        if first_any is None:
            first_any = label
    if first_en_variant is not None:
        return first_en_variant
    if first_mul is not None:
        return first_mul
    return first_any
