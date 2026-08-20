# authorities/ukhc/build_variants.py
"""Regenerate ``authorities/ukhc/name_variants.json`` — the alternative names,
abbreviations and codes for the 92 UK historic counties (place#204).

WHY THIS EXISTS. Every ``ukhc`` document carried exactly one toponym, the
canonical modern-standard county name, so any historic, abbreviated or
``-shire`` spelling matched nothing: ``Somersetshire`` returned 0 hits against a
namespace whose whole purpose is to be the containment geography historians
reconcile English county columns against. County columns in historical sources
are written overwhelmingly in exactly the forms that failed.

WHY IT IS A GENERATED TABLE AND NOT A RULE. The obvious derivation — alternate
``X`` with ``Xshire`` — is wrong in both directions, and the source data proves
it:

* Forward: only some counties take the suffix. ``Somersetshire``, ``Devonshire``
  and ``Dorsetshire`` are attested; ``Sussexshire``, ``Norfolkshire`` and
  ``Angleseyshire`` are not, and generating them would put fictional toponyms
  into the index.
* Reverse: stripping ``-shire`` yields the county town, not the county —
  ``York``, ``Bedford``, ``Lancaster``, ``Aberdeen``. Those would shadow the
  like-named cities, which is why the original ingest deliberately dropped the
  shapefile's short ``COUNTY`` field. The ``places`` toponym schema has no
  weight or type on a name (``toponym_id``/``label``/``timespans`` only), so
  there is no way to enter such a form at lower rank. It stays out.

  Checked across all 92 rows: every ``COUNTY`` value that is a genuine county
  alias rather than a settlement (``Salop``, ``Hants``, ``Berks``, ``Wilts``)
  ALSO appears in the source's ``ABBR`` field, so omitting ``COUNTY`` costs
  nothing but the settlement names. The two exceptions, ``Brecknock`` and
  ``Merioneth``, come in from Schedule 1 instead.

So the variants are collected from sources that actually attest them:

1. **HCS Schedule 1** (``Historic_Counties_Standard.pdf``) — the Historic
   Counties Trust's own table of "commonly accepted alternative spellings and
   alternative names", plus the standard's ``County of X`` form and the official
   Welsh names (marked ``*`` in the PDF). Authoritative for this dataset.
2. **The shapefile's own ``ABBR`` field** — the record-office contractions
   (``Yorks``, ``Lancs``, ``Salop``, ``Hants``, ``Northants``…). Present in the
   data all along; the ingest simply never read it.
3. **Chapman codes** (GENUKI) — the genealogists' three-letter codes. Note the
   HCS explicitly says its own codes are *distinct* from Chapman codes, so both
   are indexed; ``ROC`` legitimately maps to two HCS counties (Ross-shire and
   Cromartyshire), which is why Chapman is not a key.
4. **GeoNames**, read from WHG's own ``places`` index — the multilingual and
   Latin/Cornish/Welsh forms a modern admin record already carries. Names only:
   these are modern administrative units and are NOT co-reference links.
5. **Wikidata aliases** — historical and Gaelic/Irish forms.

Plus one curated block: riding- and parts-qualified names for Yorkshire and
Lincolnshire (place#204 option (a)); see ``RIDING_VARIANTS``.

Run (needs network + ``pdftotext``; ES access is optional):

    python -m authorities.ukhc.build_variants --es-host http://localhost:9201 \
        --es-password-file /ix1/ishi/es/config/elastic.password
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

OUT = Path(__file__).with_name("name_variants.json")

HCS_PDF = "https://historiccountiestrust.co.uk/Historic_Counties_Standard.pdf"
HCS_SHP = "https://county-borders.co.uk/UKDefinitionA_WG84_Simplified.zip"
CHAPMAN = "https://www.genuki.org.uk/big/Regions/Codes"
UA = {"User-Agent": "WHG-indexing/1.0 (stephen@docuracy.co.uk)"}

#: Riding / parts names, qualified by their county (place#204 option (a)).
#:
#: The ridings are NOT historic counties — the Historic Counties Standard says so
#: in terms ("The ridings shall not be considered to be historic counties") — and
#: neither UKDefinitionA nor UKDefinitionB contains them: those two files differ
#: only in the treatment of DETACHED PARTS and both hold the same 92 whole
#: counties. The ridings already exist as places in WHG in their own right, from
#: GBHGIS (``vob_cty`` / ``vob_rc``: East/North/West Riding, Parts of
#: Lindsey/Kesteven/Holland), so option (b) is already satisfied there.
#:
#: What is indexed here is therefore only the COUNTY-QUALIFIED form, so that a
#: county column reading "Yorks, West Riding" resolves to Yorkshire while the
#: bare "West Riding" still belongs to the riding record in ``vob_cty``.
#: Contractions in common use that the source's own ``ABBR`` field does not
#: carry. ``ABBR`` supplies one contraction per county and it is authoritative,
#: but record offices and census abstracts use more than one: ``Middx`` alongside
#: the source's ``Mdx``, ``Warws`` alongside ``Warks``. Listed rather than
#: derived — there is no rule that generates them, and inventing plausible ones
#: would put forms nobody writes into the index.
EXTRA_ABBREVIATIONS = {
    "MSX": ["Middx"],       # source ABBR = "Mdx"
    "WRW": ["Warws"],       # source ABBR = "Warks"
    "SMS": ["Som"],         # source ABBR = "Somer"
    "NHB": ["Northumb"],    # source ABBR = "Northum"
    "DRH": ["Dur"],         # source ABBR = "Durham"
    "WML": ["Westmld"],     # source ABBR = "Westm"
    "CNW": ["Corn"],        # source ABBR = "Corn" (same; harmless no-op)
}

RIDING_VARIANTS = {
    "YRK": ["Yorkshire, West Riding", "West Riding of Yorkshire", "Yorkshire West Riding",
            "Yorks, West Riding", "Yorkshire, East Riding", "East Riding of Yorkshire",
            "Yorkshire East Riding", "Yorks, East Riding", "Yorkshire, North Riding",
            "North Riding of Yorkshire", "Yorkshire North Riding", "Yorks, North Riding"],
    "LNC": ["Lincolnshire, Parts of Lindsey", "Lincolnshire, Parts of Kesteven",
            "Lincolnshire, Parts of Holland", "Lincs, Parts of Lindsey",
            "Lincs, Parts of Kesteven", "Lincs, Parts of Holland",
            "Lindsey", "Kesteven"],
}


def _get(url: str, tries: int = 3) -> bytes:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                return r.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2)


def read_shapefile_records() -> list[dict]:
    """``NAME`` / ``COUNTY`` / ``ABBR`` / ``HCS_CODE`` for the 92 counties."""
    import shapefile  # pyshp

    z = zipfile.ZipFile(io.BytesIO(_get(HCS_SHP)))
    base = next(n for n in z.namelist() if n.lower().endswith(".shp"))[:-4]
    r = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")),
                         dbf=io.BytesIO(z.read(base + ".dbf")),
                         shx=io.BytesIO(z.read(base + ".shx")))
    return [sr.record.as_dict() for sr in r.iterShapeRecords()]


def read_schedule_1() -> dict[str, dict]:
    """Parse Schedule 1 of the Standard: ``County of`` / proper / alternatives.

    ``pdftotext -layout`` preserves the columns, but their offsets shift between
    pages, so fields are matched to the nearest column anchor of the row they
    continue rather than sliced at fixed positions.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "hcs.pdf"
        pdf.write_bytes(_get(HCS_PDF))
        txt = Path(tmp) / "hcs.txt"
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
        lines = txt.read_text(encoding="utf-8", errors="replace").split("\n")

    # LAST occurrence, not the first: the table of contents carries the same
    # heading (truncated, hence no "NAMES"), and starting there yields 0 rows.
    starts = [i for i, l in enumerate(lines) if "SCHEDULE 1" in l]
    start = starts[-1]
    ends = [i for i, l in enumerate(lines) if "SCHEDULE 2" in l and i > start]
    end = ends[0] if ends else len(lines)
    field = re.compile(r"\S(?:.*?\S)?(?=\s{2,}|$)")
    row = re.compile(r"^\s*(\d{2})\s+([A-Z]{3})\b")
    rows, cur = [], None
    for ln in lines[start:end]:
        if not ln.strip() or "SCHEDULE" in ln or re.match(r"^\s*\d+\s*$", ln):
            continue
        fs = [(m.start(), m.group()) for m in field.finditer(ln) if m.group().strip()]
        m = row.match(ln)
        if m and len(fs) >= 3:
            if cur:
                rows.append(cur)
            names = fs[2:]
            cur = {"code": m.group(2), "cols": [t for _, t in names],
                   "anchors": [o for o, _ in names]}
        elif cur and fs:
            for off, txt_ in fs:
                i = min(range(len(cur["anchors"])),
                        key=lambda j: abs(cur["anchors"][j] - off))
                cur["cols"][i] = (cur["cols"][i] + " " + txt_).strip()
    if cur:
        rows.append(cur)
    out = {}
    for r in rows:
        cols = [re.sub(r"\s+", " ", c).strip() for c in r["cols"]]
        while len(cols) < 3:
            cols.append("")
        out[r["code"]] = {"county_of": cols[0], "proper": cols[1], "alt": cols[2]}
    if len(out) != 92:
        raise RuntimeError(f"Schedule 1 parse yielded {len(out)} rows, expected 92")
    return out


def read_chapman(names_by_code: dict[str, str]) -> dict[str, str]:
    """Chapman code per HCS code, matched on the county name."""
    import html

    t = _get(CHAPMAN).decode("utf-8", errors="replace")
    pairs = {}
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
        cells = [c for c in cells if c]
        if len(cells) >= 2 and re.fullmatch(r"[A-Z]{3}", cells[0]):
            pairs[cells[0]] = cells[1]

    def norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"^(co\.|county of|county)\s+", "", s)
        s = re.sub(r",.*$", "", s)
        return re.sub(r"[^a-z& ]", "", s).strip()

    by_name: dict[str, list[str]] = {}
    for code, desc in pairs.items():
        by_name.setdefault(norm(desc), []).append(code)

    # GENUKI spells four counties differently from the HCS, and folds Ross-shire
    # and Cromartyshire into one code. Stated rather than fuzzy-matched.
    MANUAL = {"BRN": "BRE",   # Brecknockshire  / GENUKI "Breconshire"
              "CRN": "CAE",   # Caernarfonshire / GENUKI "Caernarvonshire"
              "RSS": "ROC",   # Ross-shire      \ GENUKI "Ross and Cromarty"
              "CRT": "ROC",   # Cromartyshire   /
              "BTE": "BUT",   # Buteshire       \ GENUKI drops the -shire for
              "NRN": "NAI",   # Nairnshire       > these three Scottish counties
              "PRT": "PER"}   # Perthshire      /
    out = {}
    for code, name in names_by_code.items():
        if code in MANUAL:
            out[code] = MANUAL[code]
            continue
        cand = by_name.get(norm(name))
        if cand and len(cand) == 1:
            out[code] = cand[0]
        else:
            print(f"  ! no unambiguous Chapman code for {code} {name}: {cand}",
                  file=sys.stderr)
    return out


def read_geonames(names_by_code: dict[str, str], proper_by_code: dict[str, str],
                  es_host: str, es_password: str) -> dict[str, dict]:
    """Toponym inventories of the matching GB GeoNames ADM records, from WHG's
    own ``places`` index.

    Names only — a GeoNames ADM record is a MODERN administrative unit, so it is
    a good source of spellings and a bad source of co-reference. Matching is
    exact title + ``ccodes: GB`` + an ``ADM*`` type, and anything matching more
    than one record is dropped rather than guessed.
    """
    import base64

    wanted = {}
    for code, name in names_by_code.items():
        wanted.setdefault(name.casefold(), code)
        proper = proper_by_code.get(code)
        if proper:
            wanted.setdefault(proper.casefold(), code)
    body = json.dumps({
        "size": 500,
        "query": {"bool": {"filter": [
            {"term": {"namespace": "gn"}},
            {"terms": {"title.keyword": sorted({n for n in
                                                list(names_by_code.values()) +
                                                list(proper_by_code.values()) if n})}},
            {"terms": {"ccodes": ["GB"]}},
        ]}},
        "_source": ["place_id", "title", "types.identifier", "toponyms.toponym_id"],
    }).encode()
    req = urllib.request.Request(f"{es_host}/places/_search", data=body,
                                 headers={**UA, "Content-Type": "application/json"})
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"elastic:{es_password}".encode()).decode())
    with urllib.request.urlopen(req, timeout=90) as r:
        hits = json.load(r)["hits"]["hits"]

    ADM = {"ADM1", "ADM2", "ADM3", "ADM1H", "ADM2H", "ADM3H"}
    by_code: dict[str, list[dict]] = {}
    for h in hits:
        s = h["_source"]
        if not {t.get("identifier") for t in (s.get("types") or [])} & ADM:
            continue
        code = wanted.get((s.get("title") or "").casefold())
        if code:
            by_code.setdefault(code, []).append(s)
    out = {}
    for code, matches in by_code.items():
        if len(matches) != 1:
            print(f"  ! {code}: {len(matches)} GeoNames ADM matches — skipped",
                  file=sys.stderr)
            continue
        s = matches[0]
        out[code] = {"place_id": s["place_id"],
                     "toponyms": [t["toponym_id"] for t in (s.get("toponyms") or [])]}
    return out


def read_wikidata(names_by_code: dict[str, str]) -> dict[str, dict]:
    """Aliases of the Wikidata item whose own description calls it a historic
    county. Items that are ambiguous or absent under that rule are skipped —
    a label match alone is not evidence.
    """
    hist = re.compile(r"\b(historic|traditional|former)\b.{0,20}\bcount(y|ies)\b", re.I)
    ni = re.compile(r"\bcount(y|ies)\b.{0,30}\b(northern ireland|ireland|ulster)\b", re.I)
    qids = {}
    for code, name in names_by_code.items():
        q = urllib.parse.urlencode({"action": "wbsearchentities", "search": name,
                                    "type": "item", "language": "en", "uselang": "en",
                                    "limit": 10, "format": "json"})
        try:
            res = json.loads(_get(f"https://www.wikidata.org/w/api.php?{q}"))
        except Exception:
            continue
        cands = [r["id"] for r in res.get("search", []) or []
                 if hist.search(r.get("description") or "")
                 or ni.search(r.get("description") or "")]
        if len(cands) == 1:
            qids[code] = cands[0]
        time.sleep(0.12)

    out = {}
    items = list(qids.items())
    for i in range(0, len(items), 25):
        chunk = items[i:i + 25]
        ids = "|".join(q for _, q in chunk)
        url = (f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={ids}"
               f"&props=labels|aliases&languages=en|cy|ga|gd&format=json")
        try:
            ents = json.loads(_get(url)).get("entities", {})
        except Exception:
            continue
        for code, q in chunk:
            e = ents.get(q, {})
            # Keep each alias's LANGUAGE. Flattening the per-language buckets
            # tagged Irish and Welsh forms ("Contae Aontroma", "Swydd Antrim")
            # as English, which is wrong in the index and wrong for anyone
            # filtering by language.
            out[code] = {
                "qid": q,
                "aliases": [{"label": a["value"], "lang": lang}
                            for lang, entries in e.get("aliases", {}).items()
                            for a in entries],
                "labels": {k: v["value"] for k, v in e.get("labels", {}).items()},
            }
        time.sleep(0.3)
    return out


def build(es_host: str | None, es_password: str | None) -> dict:
    print("Reading the county-borders shapefile ...", file=sys.stderr)
    recs = read_shapefile_records()
    names_by_code = {r["HCS_CODE"]: r["NAME"] for r in recs}
    county_by_code = {r["HCS_CODE"]: r["COUNTY"] for r in recs}
    abbr_by_code = {r["HCS_CODE"]: r["ABBR"] for r in recs}

    print("Parsing Schedule 1 of the Historic Counties Standard ...", file=sys.stderr)
    sched = read_schedule_1()
    proper_by_code = {c: v["proper"] for c, v in sched.items()}

    print("Fetching Chapman codes ...", file=sys.stderr)
    chapman = read_chapman(names_by_code)

    print("Fetching Wikidata aliases ...", file=sys.stderr)
    wikidata = read_wikidata(names_by_code)

    geonames = {}
    if es_host and es_password:
        print("Reading GeoNames variants from the places index ...", file=sys.stderr)
        geonames = read_geonames(names_by_code, proper_by_code, es_host, es_password)

    counties = {}
    for code in sorted(names_by_code):
        name = names_by_code[code]
        seen = {name.casefold()}
        variants: list[dict] = []

        def add(label: str, lang: str, source: str) -> None:
            label = re.sub(r"\s+", " ", (label or "")).strip().strip(",")
            if not label or label.casefold() in seen:
                return
            # Never index a bare settlement short-form (see module docstring).
            if label.casefold() == county_by_code[code].casefold() \
                    and label.casefold() != abbr_by_code[code].casefold():
                return
            seen.add(label.casefold())
            variants.append({"label": label, "lang": lang, "source": source})

        s = sched.get(code, {})
        add(s.get("proper", ""), "en", "hcs-schedule1")
        for form in re.split(r",\s*", s.get("county_of", "")):
            if form.strip():
                add(f"County of {form.strip()}", "en", "hcs-schedule1")
        for form in re.split(r",\s*", s.get("alt", "")):
            form = form.strip()
            if not form:
                continue
            welsh = form.endswith("*")           # the PDF marks Welsh forms
            add(form.rstrip("*"), "cy" if welsh else "en", "hcs-schedule1")

        add(abbr_by_code[code], "en", "hcs-abbr")
        for extra in EXTRA_ABBREVIATIONS.get(code, []):
            add(extra, "en", "curated-abbr")
        add(code, "und", "hcs-code")
        if code in chapman:
            add(chapman[code], "und", "chapman-code")
        for label in RIDING_VARIANTS.get(code, []):
            add(label, "en", "riding-qualified")

        for tid in geonames.get(code, {}).get("toponyms", []):
            label, _, lang = tid.rpartition("@")
            add(label, lang or "und", "geonames")
        wd = wikidata.get(code, {})
        for alias in wd.get("aliases", []):
            add(alias["label"], alias["lang"], "wikidata")
        for lang, label in (wd.get("labels") or {}).items():
            if lang != "en":
                add(label, lang, "wikidata")

        counties[code] = {"name": name, "variants": variants}
        if code in geonames:
            counties[code]["geonames_place_id"] = geonames[code]["place_id"]
        if wd.get("qid"):
            counties[code]["wikidata_qid"] = wd["qid"]

    return {
        "_meta": {
            "description": "Alternative names, abbreviations and codes for the 92 "
                           "UK historic counties (place#204).",
            "generator": "authorities/ukhc/build_variants.py",
            "sources": {
                "hcs-schedule1": HCS_PDF,
                "hcs-abbr": HCS_SHP + " (ABBR field)",
                "hcs-code": HCS_SHP + " (HCS_CODE field)",
                "chapman-code": CHAPMAN,
                "geonames": "WHG places index, GB ADM records (names only, NOT co-reference)",
                "wikidata": "wbsearchentities + wbgetentities aliases",
                "curated-abbr": "curated; see EXTRA_ABBREVIATIONS in the generator",
                "riding-qualified": "curated; see RIDING_VARIANTS in the generator",
            },
        },
        "counties": counties,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--es-host", default=None,
                    help="ES host for the GeoNames variant pass (optional)")
    ap.add_argument("--es-password-file", default=None)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    password = None
    if args.es_password_file:
        password = Path(args.es_password_file).read_text().strip()

    data = build(args.es_host, password)
    Path(args.out).write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    n = sum(len(c["variants"]) for c in data["counties"].values())
    print(f"Wrote {args.out}: {len(data['counties'])} counties, {n} variants",
          file=sys.stderr)


if __name__ == "__main__":
    main()
