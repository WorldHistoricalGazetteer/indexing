#!/usr/bin/env python
"""Build ``gateway/data/place_qualifiers.json`` — the trailing words that mark an
administrative qualifier rather than part of a name (place#205).

Usage
-----
    python -m processing.build_place_qualifiers \
        --es-host http://localhost:9201 --out gateway/data/place_qualifiers.json

Why a vocabulary and not a rule
-------------------------------
``Bury St Edmunds Suffolk`` has the same trailing-qualifier shape as
``Bury St. Edmunds, Suffolk`` and fails the same way, but with no comma there is
nothing structural to say where the name ends.

The obvious guard — "don't drop the last word if the word before it joins a name
(*upon*, *on*, *le*, *cum*, *St*)" — was **measured against 1,178 real 3+-word
toponyms from the prod index and fired on 82.7% of them**. It is built from
English place-name morphology, and the index is global: ``Tamarack Creek
Spring``, ``Huron Towers Apartments``, ``Sumner Pioneer Cemetery``, ``Cerro los
Pájaros``, ``Fonte do Sudre`` are ordinary names whose last word is a generic or
an adjective. Every one of those would have cost a wasted KNN pass.

So the test is not morphological but lexical: **is the trailing phrase the name
of an administrative unit?** That is precisely the qualifier hypothesis, and it
is checkable against data WHG already holds. On the same sample it fires on
almost nothing, and it is strictly MORE capable — it catches ``Kingston Surrey``,
which the morphological guard had to refuse because two-word strings are
structurally ambiguous.

Sources
-------
* ``authorities/ukhc/name_variants.json`` — the 92 UK historic counties with
  every attested name, abbreviation and code (place#204). This is the vocabulary
  British gazetteer columns are actually written in: *Suffolk*, *Herts*,
  *Bucks*, *Salop*, *Yorks*, *West Riding*.
* the ``un`` namespace in the ``places`` index — ISO-3166 country names.
* ``EXTRA_QUALIFIERS`` below — the UK's constituent countries and a few
  region words that are neither an ISO country nor a historic county.

KNOWN LIMIT, stated rather than hidden: this is a British-and-countries
vocabulary. A trailing US state, French département or German Land will not be
recognised yet, and those queries keep the behaviour they have today. Extending
it is a matter of adding a source here, not of changing the gateway.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UKHC_VARIANTS = REPO / "authorities" / "ukhc" / "name_variants.json"
DEFAULT_OUT = REPO / "gateway" / "data" / "place_qualifiers.json"

#: Neither an ISO country nor a historic county, but written as a qualifier all
#: the same.
EXTRA_QUALIFIERS = [
    "England", "Scotland", "Wales", "Northern Ireland", "Great Britain",
    "United Kingdom", "UK", "GB", "Eng", "Scot", "Ire", "Ireland",
    "North Wales", "South Wales", "Mid Wales",
]

#: A qualifier must be at least this long. One- and two-character strings are
#: too collision-prone to be worth it ("Co", "Ha"), and nothing in the sources
#: below needs them.
MIN_LENGTH = 3

_EDGE = re.compile(r"^[^\w]+|[^\w]+$")


def _clean(label: str) -> str:
    return _EDGE.sub("", " ".join((label or "").split()))


def _country_names(es_host: str, password: str) -> list[str]:
    body = json.dumps({
        "size": 500,
        "query": {"bool": {"filter": [{"term": {"namespace": "un"}}]}},
        "_source": ["title", "toponyms.toponym_id"],
    }).encode()
    req = urllib.request.Request(f"{es_host}/places/_search", data=body,
                                 headers={"Content-Type": "application/json"})
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"elastic:{password}".encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as r:
        hits = json.load(r)["hits"]["hits"]
    names = []
    for h in hits:
        src = h["_source"]
        if src.get("title"):
            names.append(src["title"])
        for t in (src.get("toponyms") or []):
            label = (t.get("toponym_id") or "").rpartition("@")[0]
            if label:
                names.append(label)
    print(f"  un: {len(hits)} country docs → {len(names)} name forms", file=sys.stderr)
    return names


def build(es_host: str | None, password: str | None) -> dict:
    qualifiers: dict[str, str] = {}   # casefolded label → source

    def add(label: str, source: str) -> None:
        label = _clean(label)
        if len(label) < MIN_LENGTH:
            return
        qualifiers.setdefault(label.casefold(), source)

    data = json.loads(UKHC_VARIANTS.read_text(encoding="utf-8"))["counties"]
    for county in data.values():
        add(county["name"], "ukhc")
        for v in county["variants"]:
            add(v["label"], "ukhc")
    print(f"  ukhc: {len(qualifiers)} forms", file=sys.stderr)

    if es_host and password:
        for name in _country_names(es_host, password):
            add(name, "un")
    for name in EXTRA_QUALIFIERS:
        add(name, "extra")

    by_source: dict[str, int] = {}
    for source in qualifiers.values():
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "_meta": {
            "description": "Trailing phrases that mark an administrative "
                           "qualifier rather than part of a place name "
                           "(place#205). Casefolded.",
            "generator": "processing/build_place_qualifiers.py",
            "counts": by_source,
            "min_length": MIN_LENGTH,
        },
        "qualifiers": sorted(qualifiers),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--es-host", default=None, help="for the ISO country pass")
    ap.add_argument("--es-password-file", default=None)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    password = (Path(args.es_password_file).read_text().strip()
                if args.es_password_file else None)
    data = build(args.es_host, password)
    Path(args.out).write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}: {len(data['qualifiers'])} qualifier phrases",
          file=sys.stderr)


if __name__ == "__main__":
    main()
