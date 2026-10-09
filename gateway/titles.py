# gateway/titles.py
"""
Read-time display-title repair (place#290).

About 2.72M ``wd`` places were ingested with a bare Wikidata QID (``Q12345``)
as their ``title`` because the extractor fell back to the QID whenever the
entity had no ``en``/``mul`` label. Every consumer then showed "Q12345" as the
place's name.

The extractor now stores the preferred name (``processing/titles.py`` — the
same rule this module applies), so the next ``wd`` re-ingest retires this
repair for the records it rewrites. Until then, and for anything it does not
reach, the gateway substitutes the place's preferred toponym when the stored
title is a bare QID. The QID is not lost — it is returned alongside as
``qid_title`` so a consumer can still show or link it. Once a record's stored
title is a name, ``display_title`` returns it unchanged with ``qid_title``
None; the QID is still the ``place_id`` (``wd:Q12345``).
"""

from __future__ import annotations

from typing import Iterable, Optional

from processing.titles import is_bare_qid, preferred_label  # noqa: F401  (re-exported)


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
    chosen = preferred_label(_label_lang(n) for n in (names or ()))
    if chosen is None:
        return title, None
    return chosen, title.strip()
