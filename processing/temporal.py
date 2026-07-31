# processing/temporal.py

"""
Temporal encoding helpers — attestations, lifespans, and uncertainty bounds.

The defect this exists to prevent (place#164)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``schemas/places.json`` gives every temporal endpoint three sub-fields — ``in``,
``earliest``, ``latest`` — under both ``start`` and ``end``. Ingestion used
``in`` almost exclusively, and that one choice inverted the meaning of most of
the corpus's dates:

===================================  ==========================================
encoding                             claim
===================================  ==========================================
``start.in = Y``, ``end.in = Y``     the place existed **only** in year Y
``start.latest = Y``,                started **no later than** Y, ended **no
``end.earliest = Y``                 earlier than** Y — i.e. **attested alive at Y**
===================================  ==========================================

Same two numbers, opposite meanings. A source that records places *as they
were* at some moment — OSM's planet dump, Index Villaris in 1680 — makes the
second claim, and we stored the first. Read literally, ``osm`` said "this place
existed only in 2025", so **every historical date filter excluded it**.

Encoded correctly the problem dissolves, because the **absent** outer bounds
carry the meaning:

.. code-block::

    definitely alive at Q :  start.latest <= Q <= end.earliest
    possibly  alive at Q :  (start.earliest ?? -inf) <= Q <= (end.latest ?? +inf)

An OSM boundary is then not *definitely* alive in 1500, but **is** *possibly*
alive, because ``start.earliest`` is absent and therefore unbounded. No
``end = 9999`` sentinel, no "+Contemporary" toggle, no year heuristic.

Choosing a helper
~~~~~~~~~~~~~~~~~
This is a **per-source interpretation**, not a global rule — not everything is
an attestation:

* :func:`attested_at` — the source is a snapshot: it records the place as it
  was at one moment (``osm``, ``ohm``-the-dump-date, ``tgn``, ``nl``, ``iv``,
  Wikidata P585 "point in time").
* :func:`attested_window` — the source's evidence is a *range* and we cannot
  say where in it the place was seen (``gb`` 1888–1914 survey window, ``ofs``
  1830–1849 register compilation, ``alc`` 1786–1789 publication). The
  *definite* core is legitimately empty; that is honest, not a defect.
* :func:`lifespan` — the source states a genuine start and/or end of existence
  (``ohm``'s ``start_date``/``end_date`` tags, ``clio`` polities, ``hgis``
  admin units, Wikidata P571 inception / P576 dissolved). ``in`` is *correct*
  here. Applies the closure rule below.
* :func:`bounded` — the source is natively fuzzy and gives real outer bounds
  (``po``/PeriodO, ``vob_*`` census snapshot sequences, coarse-precision
  Wikidata dates). This is the four-field model earning its keep.

Closure rule
~~~~~~~~~~~~
**Anything that ended must have started no later than it ended**, so a known
end always licenses a ``start.latest``. This is not cosmetic: the *definitely
alive* test needs an upper bound on ``start``, so without it a record carrying
only an end can **never be definitely alive at any year** — plainly wrong for a
historic county that existed in 1973. :func:`lifespan` applies it
automatically. Prefer a real start where one is known.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "attested_at",
    "attested_window",
    "lifespan",
    "bounded",
    "coerce_year",
    "normalise_timespans",
    "precision_bounds",
    "ENDPOINTS",
    "SUBFIELDS",
]

#: The two endpoints and three sub-fields every timespan may carry.
ENDPOINTS = ("start", "end")
SUBFIELDS = ("in", "earliest", "latest")


def _clean(ts: dict[str, Any]) -> list[dict[str, Any]]:
    """Drop empty endpoints; return ``[]`` rather than a meaningless ``[{}]``."""
    out = {k: v for k, v in ts.items() if v}
    return [out] if out else []


def attested_at(year: int | None) -> list[dict[str, Any]]:
    """The source records the place *as it was* at ``year``.

    → ``start.latest = year``, ``end.earliest = year``: started no later than
    ``year``, ended no earlier than it. Definitely alive at ``year``; possibly
    alive at any time, since neither outer bound is asserted.

    This is the correct encoding for a dated snapshot — **not**
    ``start.in``/``end.in``, which claims the place existed *only* in that year.
    """
    if year is None:
        return []
    return _clean({"start": {"latest": year}, "end": {"earliest": year}})


def attested_window(first: int | None, last: int | None) -> list[dict[str, Any]]:
    """The source attests the place *somewhere within* ``[first, last]``.

    For compilation/survey/publication windows where the evidence cannot be
    pinned to a year. Started no later than ``last``, ended no earlier than
    ``first`` — so the *definite* interval (``start.latest <= Q <=
    end.earliest``) is empty whenever ``first < last``. That is the honest
    reading: the place is *possibly* alive throughout, and definitely alive at
    no single year we can name.

    Degrades to :func:`attested_at` when the window is a single year, and
    tolerates a reversed pair rather than emitting nonsense.
    """
    if first is None and last is None:
        return []
    if first is None:
        return _clean({"start": {"latest": last}})
    if last is None:
        return _clean({"end": {"earliest": first}})
    if first > last:
        first, last = last, first
    return _clean({"start": {"latest": last}, "end": {"earliest": first}})


def lifespan(
    start: int | None = None,
    end: int | None = None,
    *,
    apply_closure: bool = True,
) -> list[dict[str, Any]]:
    """A genuine lifespan: the source states when the place began and/or ended.

    ``in`` is correct here — this is the one case where it is. Where ``end`` is
    known but ``start`` is not, the **closure rule** adds
    ``start.latest = end`` (see the module docstring), without which the record
    can never test as definitely alive at any year.

    Pass ``apply_closure=False`` only when the caller supplies its own
    ``start`` bound.
    """
    ts: dict[str, Any] = {}
    if start is not None:
        ts["start"] = {"in": start}
    if end is not None:
        ts["end"] = {"in": end}
        if start is None and apply_closure:
            ts["start"] = {"latest": end}
    return _clean(ts)


def bounded(
    start_earliest: int | None = None,
    start_latest: int | None = None,
    end_earliest: int | None = None,
    end_latest: int | None = None,
) -> list[dict[str, Any]]:
    """All four bounds, for natively-fuzzy sources.

    ``start_earliest <= start <= start_latest`` and
    ``end_earliest <= end <= end_latest``, any of which may be ``None`` for
    "unbounded". Used by PeriodO (whose model *is* this shape), by the
    ``vob_*`` census-snapshot sequences, and by coarse-precision Wikidata
    dates via :func:`precision_bounds`.

    The closure rule is applied when nothing bounds ``start`` from above but an
    end is known.

    An endpoint whose two bounds coincide is **collapsed to** ``in``: bounds
    that meet pin the year exactly, which is what ``in`` means, and emitting the
    canonical form spares every consumer the choice of two encodings for one
    fact. That costs nothing — ``in`` is exact and so already has to serve as
    both bounds when read back (see
    :func:`processing.gazetteer_temporal_extent.doc_temporal_bounds`), because
    genuine lifespans use it.
    """
    start: dict[str, Any] = {}
    end: dict[str, Any] = {}
    if start_earliest is not None:
        start["earliest"] = start_earliest
    if start_latest is not None:
        start["latest"] = start_latest
    if end_earliest is not None:
        end["earliest"] = end_earliest
    if end_latest is not None:
        end["latest"] = end_latest
    if not start and end:
        closure = end_earliest if end_earliest is not None else end_latest
        if closure is not None:
            start["latest"] = closure
    for node in (start, end):
        exact = node.get("earliest")
        if exact is not None and exact == node.get("latest"):
            node.clear()
            node["in"] = exact
    return _clean({"start": start, "end": end})


def coerce_year(value: Any) -> int | None:
    """Best-effort year from an int, a bare numeric string, or an ISO date.

    LPF ``earliest``/``latest`` are "sometimes a string ISO date, sometimes a
    bare number" (``processing/staged_parquet.py``), and 208,937 ``whg`` docs
    carry ``{"start": {"earliest": "2022"}}`` as **strings** — which the reader
    silently treated as undated. Handles ``2022``, ``"2022"``, ``"2022-01-01"``,
    ``"-0049"``, ``"-0049-03-15"`` (negative years / BCE) and ``"+1200"``.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, and ``True``
    would otherwise read as year 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    negative = text.startswith("-")
    if text[0] in "+-":
        text = text[1:]
    # An ISO-ish date: take the year component only. We store years, so the
    # month/day are discarded rather than rounded.
    head = text.split("-", 1)[0]
    if not head.isdigit():
        return None
    try:
        year = int(head)
    except ValueError:
        return None
    return -year if negative else year


def normalise_timespans(timespans: Any) -> list[dict[str, Any]]:
    """Coerce every year in a timespan list to ``int``, dropping unparseable ones.

    Apply at the **write** path for any source that passes upstream temporal
    structures through verbatim — LPF ``whens`` above all. Three things go
    wrong without it (place#164):

    * **String years reach Elasticsearch.** The live index mapped
      ``geometries.timespans.start.earliest`` and ``end.latest`` as **``text``**
      where ``schemas/places.json`` declares ``integer``, because those
      sub-fields were created by *dynamic mapping* from the first values to
      arrive — and those were strings. A ``text`` field cannot be
      range-queried at all, so the dates were unusable even once read.
    * **Parquet schema inference fails.** LPF ``earliest`` is "sometimes a
      string ISO date, sometimes a bare number" across datasets
      (``processing/staged_parquet.py``), and mixed types across rows make
      pyarrow give up on the sidecar.
    * **The reader treated them as undated** — 208,937 ``whg`` docs.

    Endpoints left with no parseable year are dropped; a timespan left with
    neither endpoint is dropped entirely.
    """
    if not isinstance(timespans, list):
        return []
    out: list[dict[str, Any]] = []
    for ts in timespans:
        if not isinstance(ts, dict):
            continue
        clean: dict[str, Any] = {}
        for endpoint in ENDPOINTS:
            node = ts.get(endpoint)
            if not isinstance(node, dict):
                continue
            fields = {}
            for sub in SUBFIELDS:
                if sub not in node:
                    continue
                year = coerce_year(node.get(sub))
                if year is not None:
                    fields[sub] = year
            if fields:
                clean[endpoint] = fields
        if clean:
            out.append(clean)
    return out


# ── Wikidata precision ─────────────────────────────────────────────────────
# Wikibase time values carry a `precision` code alongside the timestamp.
# Ingestion read the timestamp and never the precision, so a century-precision
# inception (+1200-..., precision 7) became `start.in = 1200` — "began exactly
# in 1200" — where Wikidata said "some time in the 12th century".
PRECISION_YEAR = 9
PRECISION_DECADE = 8
PRECISION_CENTURY = 7
PRECISION_MILLENNIUM = 6

# Span in years covered by each coarse precision code.
_PRECISION_SPAN = {
    PRECISION_DECADE: 10,
    PRECISION_CENTURY: 100,
    PRECISION_MILLENNIUM: 1000,
}


def precision_bounds(year: int | None, precision: int | None) -> tuple[int | None, int | None]:
    """``(earliest, latest)`` for a Wikidata year at a given ``precision``.

    Year precision (9) or finer (10 month, 11 day) pins the year exactly and
    returns ``(year, year)`` — the caller should then use ``in``. Coarser codes
    return the containing decade / century / millennium.

    Wikidata writes the *start* of the coarse period (a 12th-century date is
    ``+1200-00-00`` at precision 7), so the bounds run forward from the
    truncated value. Returns ``(None, None)`` for an unknown/absent precision
    so the caller can fall back rather than invent a span.
    """
    if year is None:
        return None, None
    if precision is None:
        return None, None
    if precision >= PRECISION_YEAR:
        return year, year
    span = _PRECISION_SPAN.get(precision)
    if span is None:
        # Geological precisions (2–5) — spans of 10 kyr and up. We do not
        # pretend to represent those as year bounds.
        return None, None
    # Truncate toward negative infinity so BCE dates bound correctly.
    base = (year // span) * span
    return base, base + span - 1
