# processing/tgn_temporal.py
"""Parse temporal data out of the Getty TGN N-Triples release.

TGN *subjects* (place records) carry no inception/abolition, but the source
holds sparse temporal data in two places (verified 2026-07-13):

* **Term level** (`TGNOut_2Terms.nt`) — ``<…/tgn/term/<n>-<lang>> gvp:estStart/
  estEnd "YYYY"`` = *when that name form was in use*. Maps to a **toponym
  timespan**. (~29K date predicates.)
* **Relation level** (`TGNOut_HierarchicalRels.nt` + `…AssociativeRels.nt`) —
  a reified relation ``<…/tgn/rel/<subject>-broader-<object>>`` carries
  ``estStart``/``estEnd``/``historicFlag`` = *when the place held that
  (parent / associative) relationship*. Aggregated per subject place into a
  **place temporal extent**. (~1.5K places.)

Shared by ``authorities/tgn-places.py`` (ingestion) and
``processing/tgn_temporal_backfill.py`` (one-time live patch) so the two agree.
"""

from __future__ import annotations

import re
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from processing.temporal import attested_at, bounded, lifespan

# gYear literal on a term URI: <…/tgn/term/8-en> <…#estStart> "1800"^^<…gYear>
_TERM_DATE_RE = re.compile(
    r'<([^>]+/tgn/term/[^>]+)>\s+<[^>]*#(estStart|estEnd)>\s+"([^"]+)"')
# subject place id from a reified relation URI: …/tgn/rel/<subject>-<reltype>-<object>
_REL_SUBJECT_RE = re.compile(r"/tgn/rel/(\d+)-")
_REL_START_RE = re.compile(r'#estStart>\s+"([^"]+)"')
_REL_END_RE = re.compile(r'#estEnd>\s+"([^"]+)"')

def _default_release_year() -> int:
    """Year of the TGN release on disk, else the current year.

    Was a hardcoded ``2025``, then a private mtime-and-fallback copy. Now
    delegates to `processing.temporal.source_release_year`, which is the single
    implementation of this convention — see its docstring for why three of them
    existed and why the fallback announces itself.
    """
    from processing.temporal import source_release_year
    try:
        from processing.settings import DATA_DIR
        release = Path(DATA_DIR) / "authorities" / "tgn" / "explicit.zip"
    except Exception:
        release = None
    return source_release_year(release, label="tgn")


#: Year the ingestion attests undated TGN records to (the release year).
PLACEHOLDER_YEAR = _default_release_year()


def parse_gyear(s: str) -> int | None:
    """XSD gYear → int. Handles zero-padded + negative (BCE) years: ``-0015`` → -15."""
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None


def timespan(start: int | None, end: int | None,
             placeholder: int = PLACEHOLDER_YEAR) -> list[dict]:
    """Build a ``timespans`` list from optional Getty bounds.

    Rewritten for place#164 — every branch below used to emit
    ``{"start": {"in": s}, "end": {"in": e}}``, i.e. a closed lifespan, even
    where TGN asserted nothing of the kind. The undated branch was the worst:
    it claimed each of ~3 M TGN places "existed only in 2025", so every
    historical date filter excluded the entire gazetteer.

    ==================  ===========================================================
    Getty gives         we now record
    ==================  ===========================================================
    start **and** end   a genuine lifespan (``in``/``in``) — the one correct use
    start only          began then, and *attested* still extant at the dump year
    end only            ended then; closure rule bounds the start (``start.latest``)
    neither             *attested* at the dump year — no claim about begin/end
    ==================  ===========================================================
    """
    if start is None and end is None:
        # Undated: all we know is Getty listed it in this release.
        return attested_at(placeholder)
    if start is None:
        # An end with no start. The closure rule supplies `start.latest`,
        # without which the record can never be *definitely* alive at any year.
        return lifespan(end=end)
    if end is None:
        # Began at `start` and Getty still lists it: attested alive at the
        # dump year, but NOT claimed to have ended there.
        return bounded(start_earliest=start, start_latest=start,
                       end_earliest=placeholder)
    if start > end:
        start, end = end, start
    return lifespan(start, end)


def parse_term_dates(zip_path: Path) -> dict[str, tuple[int | None, int | None]]:
    """``{term_uri: (start, end)}`` from term-level estStart/estEnd."""
    dates: dict[str, list] = defaultdict(lambda: [None, None])
    with zipfile.ZipFile(zip_path) as z, z.open("TGNOut_2Terms.nt") as f:
        for raw in f:
            if b"estStart" not in raw and b"estEnd" not in raw:
                continue
            m = _TERM_DATE_RE.match(raw.decode("utf-8", "replace"))
            if not m:
                continue
            uri, which, val = m.group(1), m.group(2), m.group(3)
            y = parse_gyear(val)
            if y is None:
                continue
            dates[uri][0 if which == "estStart" else 1] = y
    return {k: (v[0], v[1]) for k, v in dates.items()}


# One 2Terms pass, three line shapes (mirrors authorities/tgn-places.py):
#   term literal:  <term_uri> <…#literalForm> "name"@lang
#   concept→term:  <…/tgn/<concept>> <…#prefLabelGVP|altLabel|prefLabel> <term_uri>
#   term date:     handled by _TERM_DATE_RE above
_LITERAL_RE = re.compile(r'<([^>]+/tgn/term/[^>]+)>\s+<[^>]*#literalForm>\s+"((?:[^"\\]|\\.)*)"(?:@(\S+?))?\s*\.')
_CONCEPT_TERM_RE = re.compile(
    r'<([^>]+/tgn/(\d+))>\s+<[^>]*#(?:prefLabelGVP|altLabel|prefLabel)>\s+<([^>]+/tgn/term/[^>]+)>')


def _decode_nt(s: str) -> str:
    """Decode N-Triples \\uXXXX / \\UXXXXXXXX escapes."""
    s = re.sub(r'\\u([0-9A-Fa-f]{4})', lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r'\\U([0-9A-Fa-f]{8})', lambda m: chr(int(m.group(1), 16)), s)


def parse_concept_toponym_dates(zip_path: Path) -> dict[str, dict[str, tuple[int | None, int | None]]]:
    """``{concept_id: {toponym_id "name@lang": (start, end)}}`` — term-level
    name-in-use dates joined to their concept + toponym id (one 2Terms pass)."""
    term_literal: dict[str, tuple[str, str]] = {}     # term_uri → (name, lang)
    term_date: dict[str, list] = defaultdict(lambda: [None, None])
    concept_terms: dict[str, list[str]] = defaultdict(list)  # concept_id → [term_uri]
    with zipfile.ZipFile(zip_path) as z, z.open("TGNOut_2Terms.nt") as f:
        for raw in f:
            if b"estStart" in raw or b"estEnd" in raw:
                m = _TERM_DATE_RE.match(raw.decode("utf-8", "replace"))
                if m:
                    y = parse_gyear(m.group(3))
                    if y is not None:
                        term_date[m.group(1)][0 if m.group(2) == "estStart" else 1] = y
                continue
            if b"literalForm" in raw:
                m = _LITERAL_RE.match(raw.decode("utf-8", "replace"))
                if m:
                    term_literal[m.group(1)] = (_decode_nt(m.group(2)), m.group(3) or "")
            elif b"Label" in raw:
                m = _CONCEPT_TERM_RE.match(raw.decode("utf-8", "replace"))
                if m:
                    concept_terms[m.group(2)].append(m.group(3))

    out: dict[str, dict[str, tuple]] = defaultdict(dict)
    for concept_id, term_uris in concept_terms.items():
        for term_uri in term_uris:
            d = term_date.get(term_uri)
            lit = term_literal.get(term_uri)
            if not d or not lit:
                continue
            name, lang = lit
            out[concept_id][f"{name}@{lang}"] = (d[0], d[1])
    return {k: v for k, v in out.items() if v}


def parse_relation_dates(zip_path: Path) -> dict[str, tuple[int | None, int | None, bool]]:
    """``{concept_id: (min_start, max_end, historic)}`` aggregated across a
    place's dated hierarchical + associative relations."""
    agg: dict[str, list] = defaultdict(lambda: [None, None, False])
    with zipfile.ZipFile(zip_path) as z:
        for member in ("TGNOut_HierarchicalRels.nt", "TGNOut_AssociativeRels.nt"):
            with z.open(member) as f:
                for raw in f:
                    has_s = b"estStart" in raw
                    has_e = b"estEnd" in raw
                    has_h = b"historicFlag" in raw
                    if not (has_s or has_e or has_h):
                        continue
                    line = raw.decode("utf-8", "replace")
                    ms = _REL_SUBJECT_RE.search(line)
                    if not ms:
                        continue
                    rec = agg[ms.group(1)]
                    if has_s:
                        mm = _REL_START_RE.search(line)
                        y = parse_gyear(mm.group(1)) if mm else None
                        if y is not None:
                            rec[0] = y if rec[0] is None else min(rec[0], y)
                    if has_e:
                        mm = _REL_END_RE.search(line)
                        y = parse_gyear(mm.group(1)) if mm else None
                        if y is not None:
                            rec[1] = y if rec[1] is None else max(rec[1], y)
                    if has_h:
                        rec[2] = True
    return {k: (v[0], v[1], v[2]) for k, v in agg.items()}
