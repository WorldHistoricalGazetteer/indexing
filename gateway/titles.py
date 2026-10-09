# gateway/titles.py
"""
Read-time display-title repair (place#290).

About 2.72M ``wd`` places were ingested with a bare Wikidata QID (``Q12345``) as
their ``title`` because the entity had no label in the extract's preferred
language. Every consumer then showed "Q12345" as the place's name.

The proper fix is at ingest (still open on place#290). Until then the gateway
substitutes the place's preferred toponym when the stored title is a bare QID:
the first ``en`` name, else the first name. The QID is not lost — it is returned
alongside as ``qid_title`` so a consumer can still show or link it.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

_QID_RE = re.compile(r"^Q\d+$")


def is_bare_qid(title: Optional[str]) -> bool:
    return bool(title) and bool(_QID_RE.match(title.strip()))


def _label_lang(name) -> tuple[str, Optional[str]]:
    if isinstance(name, dict):
        return (name.get("label") or ""), name.get("lang")
    return (getattr(name, "label", "") or ""), getattr(name, "lang", None)


def display_title(title: Optional[str], names: Iterable) -> tuple[str, Optional[str]]:
    """``(title_to_show, qid_or_None)``.

    ``names`` may be dicts or objects with ``label``/``lang``. When the stored
    title is not a bare QID, or no usable name exists, the stored title is
    returned unchanged and the second element is None.
    """
    title = title or ""
    if not is_bare_qid(title):
        return title, None
    first = None
    for n in names or ():
        label, lang = _label_lang(n)
        label = label.strip()
        if not label or is_bare_qid(label):
            continue
        if lang and lang.lower().split("-")[0] == "en":
            return label, title.strip()
        if first is None:
            first = label
    if first is not None:
        return first, title.strip()
    return title, None
