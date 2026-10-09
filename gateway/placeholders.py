# gateway/placeholders.py
"""
Placeholder toponyms in name-match discovery (place#216).

Some indexed names are cataloguing placeholders, not toponyms: GNIS's
``Oknoname 037068 Dam`` ("OK, no name"), OSM's ``Unnamed`` / ``?`` / ``fixme``,
Wikidata's ``unnamed shipwreck``. The records are real features with real
coordinates and belong in the index — but there is no query string for which a
placeholder is the right *lexical* answer, so such a name should not put its
place into the name-match candidate pool, or should do so only weakly.

This module is the single place that decides. Discovery runs over the
``toponyms`` index, so the decision is taken on the toponym ``name`` that
matched, not on the place's display ``title``: a place titled ``Unknown`` that
also carries the toponym ``Halton Holegate`` (``kain_par:11771``) is still found
by its real name, and only the placeholder stops matching. Nothing here touches
``/api/places``, ``/api/entity``, spatial scope or map tiles.

Rules live in a JSON file (``PLACEHOLDER_NAME_PATTERNS_FILE``, default
``gateway/data/placeholder_names.json``)::

    {"exclude": ["oknoname.*"], "demote": ["unknown"], "demote_weight": 0.25}

Each entry is a regular expression matched case-insensitively against the
WHOLE trimmed name (``fullmatch``), so ``no ?name`` matches ``Noname`` and not
``No Name Creek`` — 235 GNIS streams really are called that. ``exclude`` drops
the name from discovery altogether; ``demote`` multiplies its contribution by
``demote_weight``. A name matching both is excluded.

The committed file carries the tiers the Technical Director approved on
place#216 (2026-10-09): census 2's T1 as ``exclude``, T2 as ``demote``. The
rule set is cached for the process lifetime — restart the
gateway to pick up a change, matching the deploy model of the other data
files. A missing or unreadable file, or a pattern that does not compile, must
never take discovery down: bad patterns are logged and skipped.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .config import PLACEHOLDER_NAME_PATTERNS_FILE

logger = logging.getLogger("gateway.placeholders")

#: Contribution multiplier for a demoted name when the file names none.
DEFAULT_DEMOTE_WEIGHT = 0.25


@dataclass(frozen=True)
class PlaceholderRules:
    """Compiled rule set. ``weight(name)`` is the only thing discovery asks."""

    exclude: tuple[re.Pattern, ...] = ()
    demote: tuple[re.Pattern, ...] = ()
    demote_weight: float = DEFAULT_DEMOTE_WEIGHT
    source: Optional[str] = None
    #: Patterns that failed to compile, by (list, pattern) — surfaced so a typo
    #: in the file is visible in a test or a log line, not silently inert.
    rejected: tuple[tuple[str, str], ...] = field(default=())

    @property
    def active(self) -> bool:
        return bool(self.exclude or self.demote)

    def classify(self, name: Optional[str]) -> Optional[str]:
        """``"exclude"``, ``"demote"`` or ``None`` for a toponym name."""
        if not self.active or not name:
            return None
        text = name.strip()
        if not text:
            return None
        for rx in self.exclude:
            if rx.fullmatch(text):
                return "exclude"
        for rx in self.demote:
            if rx.fullmatch(text):
                return "demote"
        return None

    def weight(self, name: Optional[str]) -> float:
        """Multiplier for this name's discovery contribution: 0.0 (excluded),
        ``demote_weight`` (demoted) or 1.0 (an ordinary toponym)."""
        kind = self.classify(name)
        if kind == "exclude":
            return 0.0
        if kind == "demote":
            return self.demote_weight
        return 1.0


EMPTY_RULES = PlaceholderRules()


def _compile(patterns, which: str, rejected: list[tuple[str, str]]) -> tuple[re.Pattern, ...]:
    out: list[re.Pattern] = []
    for p in patterns or ():
        if not isinstance(p, str) or not p.strip():
            rejected.append((which, repr(p)))
            continue
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as exc:
            logger.warning("placeholder pattern %r in %r rejected: %s", p, which, exc)
            rejected.append((which, p))
    return tuple(out)


def rules_from_dict(data, source: Optional[str] = None) -> PlaceholderRules:
    """Build a rule set from the file's parsed JSON. Tolerant of a bad file:
    anything unusable is skipped and recorded in ``rejected``."""
    if not isinstance(data, dict):
        logger.warning("placeholder rules at %s are not a JSON object — ignored", source)
        return PlaceholderRules(source=source, rejected=(("file", "not an object"),))
    rejected: list[tuple[str, str]] = []
    exclude = _compile(data.get("exclude"), "exclude", rejected)
    demote = _compile(data.get("demote"), "demote", rejected)
    weight = data.get("demote_weight", DEFAULT_DEMOTE_WEIGHT)
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        rejected.append(("demote_weight", repr(weight)))
        weight = DEFAULT_DEMOTE_WEIGHT
    if not 0.0 <= weight < 1.0:
        # ≥ 1 is not a demotion and < 0 would invert the ranking.
        rejected.append(("demote_weight", repr(weight)))
        weight = DEFAULT_DEMOTE_WEIGHT
    return PlaceholderRules(exclude=exclude, demote=demote, demote_weight=weight,
                            source=source, rejected=tuple(rejected))


def load_rules_from(path: Path | str) -> PlaceholderRules:
    """Read and compile one rules file. Never raises."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("placeholder rules file %s absent — name-match "
                    "placeholder handling is off", path)
        return PlaceholderRules(source=str(path))
    except (OSError, ValueError) as exc:
        logger.warning("placeholder rules at %s unreadable (%s) — handling is off",
                       path, exc)
        return PlaceholderRules(source=str(path), rejected=(("file", str(exc)),))
    rules = rules_from_dict(data, source=str(path))
    if rules.active:
        logger.info("placeholder rules loaded from %s: %d exclude, %d demote "
                    "(weight %.2f)", path, len(rules.exclude), len(rules.demote),
                    rules.demote_weight)
    return rules


@lru_cache(maxsize=1)
def load_placeholder_rules() -> PlaceholderRules:
    """The configured rule set, cached for the process lifetime (restart the
    gateway to pick up an edited file — same model as ``clustering_params``)."""
    return load_rules_from(PLACEHOLDER_NAME_PATTERNS_FILE)
