"""Build Linked Places Format + a Map-your-Data spreadsheet from the KGLD package.

Reads the published Zenodo zip (``kyrgyzstan_lakes_dataset_v1.0.0.zip``, alongside this
file) and writes two artefacts into the same directory:

* ``kgld_v1.0.0.lpf.json`` — canonical LPF FeatureCollection, 71 features. What WHG would
  ingest if we did the conversion ourselves.
* ``kgld_v1.0.0_myd.csv``  — flat one-row-per-lake form for the Map-your-Data trial, whose
  importer is column-oriented.

Both are derived, never hand-edited: re-run this rather than patching the outputs.

Design decisions are documented in ``README-lpf.md`` and justified in ``ANALYSIS.md``.
The two that most affect the shape of the output:

* **No ``relations``.** ``admin1``/``admin2`` are oblast/district *names*, and LPF's
  ``relationTo`` must be a URL or namespace term. They stay as properties/columns so the
  contributor can reconcile them in MyD, which is what a container column is for.
* **``when`` only where the evidence supports it.** 65 of 73 lakes are undated physical
  geography; the short-lived glacial lakes genuinely formed and drained on record. Emitting
  a date for the rest would be the exact misrepresentation this whole exercise avoids.

Usage:  python3 authorities/kgld/build_lpf.py [--validate]
"""

import argparse
import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path

DIR = Path(__file__).resolve().parent
ZIP = DIR / "kyrgyzstan_lakes_dataset_v1.0.0.zip"
ZENODO = DIR / "zenodo_record_22178862.json"   # verbatim from the Zenodo REST API, 2 Sep 2026
DOI = "https://doi.org/10.5281/zenodo.22178862"
PROJECT_PAGE = "https://kyrgyzstanplanner.com/lakes-of-kyrgyzstan/"
VERSION = "1.0.0"

# Excluded from the gazetteer output — see ANALYSIS.md "What to exclude". Both are
# record_status=disputed provenance placeholders whose titles read as place names, which is
# precisely why indexing them would be wrong.
EXCLUDE = {
    "KGLD-L0011": "legacy ambiguous Merzbacher record; superseded by L0028/L0029",
    "KGLD-L0069": "unresolved Kulun FAO inventory row; explicitly not mapped to Main Kulun",
}

# ── AAT typing ────────────────────────────────────────────────────────────────────────
# Measured against the LIVE `types` index (types_20260404_150351) on 2 Sep 2026, not
# guessed: the whole lake branch under aat:300008680 is
#   300008682 oxbow lakes · 300132303 tarns · 300266561 crater lakes
#   300387021 underground lakes · 300387086 intermittent lakes
# plus 300263360 artificial lakes, 300266556 salt lakes, 300387087 dry lakes elsewhere,
# and 300132301 lacustrine bodies of water as the parent.
#
# **AAT has no concept for a glacial lake of any kind.** Nothing corresponds to KGLD's
# moraine_dammed_glacial, riegel, ice_dammed, landslide_dammed, proglacial or intramorainic
# — so there is exactly ONE applicable concept for almost every record, and refinement is
# not a task to leave to the contributor. We prefill it.
AAT_LAKES = ("aat:300008680", "lakes (bodies of water)")

# The single honest refinement available. KGLD says outright which lakes are not permanent;
# that IS what AAT means by an intermittent lake, so it is evidence, not inference. Keyed on
# (property, value) so a future value cannot match by accident.
AAT_INTERMITTENT = ("aat:300387086", "intermittent lakes")
INTERMITTENT_MARKERS = {
    ("permanence", "non_permanent"),
    ("permanence", "cyclic_seasonal"),
    ("lake_behavior", "short_lived"),
    ("lake_behavior", "seasonally_recurrent"),
    ("hydrologic_variability", "strongly_variable_intermittent"),
}

# Deliberately NOT auto-assigned, though a case exists for each:
#   300132303 tarns      — a tarn is specifically a cirque lake; KGLD's categories are
#                          moraine-dammed / riegel / ice-dammed, which are not the same.
#   300266556 salt lakes — Issyk-Kul is brackish, but KGLD does not classify it as saline
#                          and we do not add facts the source withholds.
# Both are good questions for the contributor inside MyD, where he can see the hierarchy.

# ── Pre-existing reconciliations ──────────────────────────────────────────────────────
# KGLD's source registry cites two gazetteer records as the ORIGIN of a lake's coordinate.
# Taking a coordinate (and, for Kel-Suu, an elevation) from a gazetteer record as your
# lake's own is an identity assertion: you cannot do it without having judged that the
# record denotes the same lake. So these are reconciliations already made, not merely
# citations, and they belong in `links[]` where WHG's clustering can see them — otherwise
# the two lakes in this dataset that ARE already reconciled arrive looking unreconciled.
#
# Marked `less-certain` because the inference is OURS, not his: he never wrote "sameAs".
# Each carries a citation stating the basis so a reviewer can check it, and the covering
# letter asks him to confirm or correct both.
HARD_LINKS = {
    # Source KGLD-S0019 is titled "Köl-Suu (Wikidata Q13642455; coordinate statement
    # attributed to Geographic Names Server)" and supplies L0005's coordinate + elevation.
    "KGLD-L0005": [("wd:Q13642455",
                    "KGLD source S0019 takes this lake's coordinate and elevation from "
                    "Wikidata Q13642455")],
    # Source KGLD-S0021 cites a GeoNames *search page*, not a record id, but names two
    # candidate points and KGLD adopts 40.542511,74.313168. Verified 2 Sep against the live
    # WHG index: gn:8403583 "Ozero Kulun" lies within 200 m of that exact coordinate, and
    # is the only GeoNames record that does.
    "KGLD-L0006": [("gn:8403583",
                    "KGLD source S0021 adopts the GeoNames point 40.542511,74.313168; "
                    "gn:8403583 'Ozero Kulun' is within 200 m of it")],
}

# Ramsar site identifiers are real, resolvable and in the data as classifications — but a
# Ramsar SITE is not the lake. KGLD's own note says the site boundary may include
# terrestrial protected area beyond the water, so this is `seeAlso`, never `closeMatch`.
RAMSAR_RIS = "https://rsis.ramsar.org/ris/"

MORPHO_LABEL = {
    "AREA_KM2": ("surface area", "km²"),
    "ELEVATION_M": ("elevation", "m"),
    "MAX_DEPTH_M": ("maximum depth", "m"),
    "MEAN_DEPTH_M": ("mean depth", "m"),
    "VOLUME_KM3": ("volume", "km³"),
    "MAX_LENGTH_KM": ("maximum length", "km"),
    "MAX_WIDTH_KM": ("maximum width", "km"),
    "CATCHMENT_KM2": ("catchment area", "km²"),
    "SHORELINE_KM": ("shoreline length", "km"),
}


def read_csv(zf, name):
    """KGLD ships UTF-8 with a BOM; utf-8-sig or the first column name carries it."""
    with zf.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))


def load():
    zf = zipfile.ZipFile(ZIP)
    return {
        "lakes": read_csv(zf, "data/kyrgyzstan_lakes.csv"),
        "names": read_csv(zf, "data/kyrgyzstan_lake_names.csv"),
        "classifications": read_csv(zf, "data/kyrgyzstan_lake_classifications.csv"),
        "measurements": read_csv(zf, "data/kyrgyzstan_lake_measurements.csv"),
        "reference_public": read_csv(zf, "data/kyrgyzstan_lake_reference_public.csv"),
        "events": read_csv(zf, "data/kyrgyzstan_glacial_lake_events.csv"),
        "observations": read_csv(zf, "data/kyrgyzstan_lake_observations.csv"),
        "sources": read_csv(zf, "metadata/kyrgyzstan_lake_sources.csv"),
        "study_features": read_csv(zf, "data/kyrgyzstan_lake_study_features.csv"),
    }


def source_label(src, sid):
    """A citation label a reader can chase, from KGLD's own source registry."""
    s = src.get(sid)
    if not s:
        return None
    bits = [b for b in (s.get("authors_or_organization"), s.get("title")) if b]
    label = " — ".join(bits) or sid
    if s.get("publication_year"):
        label += f" ({s['publication_year']})"
    return label


def year_of(datestr):
    d = (datestr or "").strip()
    return d[:4] if len(d) >= 4 and d[:4].isdigit() else None


def build_when(lake_id, events, observations):
    """A feature-level `when` ONLY where the lake's own existence is dated.

    The short-lived glacial lakes formed and drained on record, so a lifespan is a real
    claim about the place. For a permanent lake there is no such claim to make, and the
    function returns None rather than inventing one. Observation dates bound the span where
    a drainage event exists, so a lake seen in 2008 and drained in 2008 gets that year, not
    the publication year of the study.
    """
    ev = [e for e in events if e["lake_id"] == lake_id]
    obs = [o for o in observations if o["lake_id"] == lake_id]
    if not ev:
        return None                     # no documented formation/drainage → undated place
    starts, ends = [], []
    for o in obs:
        y = year_of(o.get("observation_date"))
        if y:
            starts.append(y)
    for e in ev:
        y = year_of(e.get("event_end_date")) or year_of(e.get("event_start_date"))
        if y:
            ends.append(y)
    if not (starts or ends):
        return None
    ts = {}
    if starts:
        ts["start"] = {"in": min(starts)}
    if ends:
        ts["end"] = {"in": max(ends)}
    if not ts:
        return None
    return {"timespans": [ts], "certainty": "less-certain",
            "label": "documented formation/drainage episode"}


def observation_years(lake_id, measurements, observations):
    """Distinct years in which KGLD observed this lake, for the contributor's convenience.

    Emitted as `properties.observation_years` / the CSV's `observation_years` column, and
    it makes **no claim about the place** — it is a lookup, not a date. Its purpose is the
    capture date: when he places a lake in Map your Data, the geometry wants a
    `Geometry captured (date)`, and for 45 of the 71 lakes his own data already supplies
    candidate years rather than making him invent one.

    Deliberately NOT promoted to a feature-level `when`. These are attestations — someone
    measured the lake in year Y — and a lake measured in 1911 and 2009 existed before and
    after both. `reduce_timespan_to_years` in WHG's ingest collapses any timespan to
    [min, max] and `minmax` drives the map's temporal filter, so emitting these as a place
    date would make Petrov Lake vanish from today's map. See ANALYSIS.md, "Why only 5
    features carry a `when`".

    Event years are excluded: for the 5 lakes that have them they are already the
    feature-level `when`, and a drainage date is not a candidate capture date.
    """
    years = set()
    for m in measurements:
        if m["lake_id"] == lake_id:
            y = year_of(m.get("observation_year")) or year_of(m.get("observation_start"))
            if y:
                years.add(y)
    for o in observations:
        if o["lake_id"] == lake_id:
            y = year_of(o.get("observation_date"))
            if y:
                years.add(y)
    return sorted(years)


def morphometry_sentence(lake_id, reference_public, src):
    """Prose carrier for the numbers the places schema has no structured home for.

    Deliberately a summary with its source named, not a pretence of structured data —
    ANALYSIS.md "Fields with no home in the places schema" explains why the evidence layer
    stays at Zenodo.
    """
    row = next((r for r in reference_public if r["lake_id"] == lake_id), None)
    if not row:
        return None
    parts, cited = [], set()
    for var, key in (("AREA_KM2", "area_km2"), ("ELEVATION_M", "elevation_m"),
                     ("MAX_DEPTH_M", "max_depth_m"), ("VOLUME_KM3", "volume_km3")):
        val = (row.get(key) or "").strip()
        if not val:
            continue
        label, unit = MORPHO_LABEL[var]
        parts.append(f"{label} {val} {unit}")
        sid = (row.get(key.rsplit("_", 1)[0] + "_source_id") or "").strip()
        for s in sid.split(";"):
            if s.strip():
                cited.add(s.strip())
    if not parts:
        return None
    text = "Published reference values: " + "; ".join(parts) + "."
    labels = [source_label(src, s) for s in sorted(cited)]
    labels = [x for x in labels if x]
    if labels:
        text += " Sources: " + " | ".join(labels) + "."
    if (row.get("conflict_flag") or "").upper() == "TRUE":
        text += (" Note: KGLD records a source conflict for at least one of these values; "
                 "the full evidence and conflict register are in the Zenodo release.")
    return text


def _name_node(full):
    """CSL/schema.org name split. WHG's csl-citation schema REQUIRES `family` on every
    author — it does not accept a literal-only name — so an organisation or single-token
    name goes into `family` too. Mirrors MyD's own `cslNameNode`."""
    if "," in full:
        family, given = full.split(",", 1)
        return {"family": family.strip(), "given": given.strip()}
    return {"family": full.strip()}


def citation_blocks():
    """The two metadata blocks MyD writes at the top of every exported/contributed LPF,
    here populated from the Zenodo record rather than from a browser form.

    * ``indexing``  — schema.org Dataset. **This is the one WHG's ingest actually reads**
      (``validation.views.extract_dataset_metadata``): creator → Dataset.creator, name →
      title, description → description, url → webpage, citation → Dataset.citation.
    * ``citation``  — CSL-JSON, the format ``lpf_v2.0.jsonld`` natively `$ref`s
      (``csl-citation.json``). Not consumed on ingest yet; carried so it can be.

    Both must sit **ahead of** ``features`` so the server's streaming ijson reader stops
    early instead of walking the whole array. Every level of the CSL block satisfies
    ``additionalProperties: false``.
    """
    z = json.loads(ZENODO.read_text(encoding="utf-8"))["metadata"]
    title = z["title"]
    year = z["publication_date"][:4]
    version = z.get("version", VERSION)
    creators = [c["name"] for c in z.get("creators", [])]
    authors = "; ".join(creators)
    lic = "https://creativecommons.org/licenses/by/4.0/"

    # Strip the HTML Zenodo stores its abstract in; keep it to the ingest column's appetite.
    desc = re.sub(r"<[^>]+>", " ", z.get("description", ""))
    desc = re.sub(r"\s+", " ", desc).strip()

    formatted = (f"{authors} ({year}). {title} (Version {version}) [Data set]. "
                 f"Zenodo. {DOI}")

    indexing = {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "name": title,
        "description": desc,
        "datePublished": year,
        "version": version,
        "publisher": {"@type": "Organization", "name": "Zenodo"},
        "license": lic,
        "identifier": DOI,
        "url": DOI,
        "creator": ([{"@type": "Person", **_name_node(c)} for c in creators][0]
                    if len(creators) == 1 else
                    [{"@type": "Person", **_name_node(c)} for c in creators]),
        "citation": formatted,
    }

    slug = "kyrgyzstan-lakes-dataset"
    item = {
        "type": "dataset",
        "id": slug,
        "title": title,
        "author": [_name_node(c) for c in creators],
        "issued": {"date-parts": [[int(year)]]},
        "publisher": "Zenodo",
        "version": str(version),
        "DOI": z["doi"],
        "URL": DOI,
        "license": lic,
    }
    if z.get("keywords"):
        item["keyword"] = ", ".join(z["keywords"])
    citation = {
        "schema": "https://whgazetteer.org/schema/csl-citation.json",
        "citationID": "whg-" + slug,
        "citationItems": [{"id": slug, "itemData": item}],
    }
    return indexing, citation, formatted


# ── Study-local supraglacial features (SG, 2 Sep) ─────────────────────────────────────
# KGLD marks these `identity_status = study_local_feature` and declines to promote them to
# "permanent geographic entities". That is a physical-geography objection, and a HISTORICAL
# gazetteer is precisely where temporally-scoped entities belong — we already carry five of
# KGLD's short-lived lakes on exactly that basis, four of them without coordinates. So the
# only real obstacle was naming: "Southern Inylchek supraglacial Lake 1" is one paper's
# figure numbering, unsearchable and unmatchable.
#
# SG's resolution: give them names, because a name is what anyone calls something. They are
# named for the dataset's compiler, keyed to KGLD's own study-local numbering so the link
# back to the source figure survives. The coinage is OURS and every name says so in its
# citation — a gazetteer may coin a name, but never pass one off as attested usage.
NAME_PREFIX = "Hamilton"
NAME_COINAGE = (
    "Name coined by the World Historical Gazetteer for this contribution, after Ethan "
    "Hamilton, compiler of the Kyrgyzstan Lakes Dataset; the numeral is KGLD's own "
    "study-local lake number. Not a local, historical or otherwise attested toponym."
)


def build_study_features(data, src):
    """The 9 Southern Inylchek supraglacial lakes, as dated ephemeral places.

    Each carries a coined name (see NAME_COINAGE), the source's own study label as an
    alternative, a lifespan from its observed drainage/recharge dates, and the intermittent
    type — these fill and empty by definition. No geometry: the source publishes none.

    `parent_glacier` stays a PROPERTY, not a `relations` entry. LPF's `relationTo` needs a
    URL or namespace term, and the live index holds two GeoNames records both titled plainly
    "Inylchek Glacier" (gn:1526997 at 42.158N, gn:1527406 at 42.245N) with nothing but
    latitude to separate North from South. Guessing which is the parent would assert a
    containment we cannot support; the contributor knows the region and can reconcile it in
    Map your Data, exactly as he does for `oblast`.
    """
    feats, rows = [], []
    for sf in data["study_features"]:
        fid, local = sf["feature_id"], sf["study_local_id"]
        label = sf["feature_label"]
        coined = f"{NAME_PREFIX} {local}"

        # Lifespan from every dated observation and event touching this feature.
        dates = [o["observation_date"] for o in data["observations"]
                 if o.get("feature_id") == fid and o.get("observation_date")]
        evs = [e for e in data["events"] if fid in (e.get("feature_ids") or "").split(";")]
        for e in evs:
            dates += [d for d in (e.get("event_start_date"), e.get("event_end_date")) if d]
        dates = sorted(d for d in dates if d)

        when = None
        if dates:
            ts = {"start": {"in": dates[0]}, "end": {"in": dates[-1]}}
            when = {"timespans": [ts], "certainty": "less-certain",
                    "label": "observed drainage/recharge cycles"}

        props = {"title": coined, "ccodes": ["KG"], "kgld_id": fid,
                 "feature_type": sf["feature_type"], "parent_glacier": sf["parent_glacier"],
                 "study_local_id": local, "study_year_scope": sf["study_year_scope"],
                 "study_label": label}
        years = sorted({d[:4] for d in dates})
        if years:
            props["observation_years"] = ",".join(years)

        feat = {
            "@id": f"{DOI}#{fid}",
            "type": "Feature",
            "properties": props,
            "names": [
                {"toponym": coined, "lang": "en",
                 "citations": [{"label": NAME_COINAGE, "@id": DOI}]},
                {"toponym": label, "lang": "en",
                 "citations": [{"label": (source_label(src, sf["source_id"]) or sf["source_id"])
                                + f" — {sf['source_locator']}"}]},
            ],
            # AAT has no supraglacial-lake concept, and its scope notes do not quite
            # reach these: 300008680 is "bodies of fresh or salt water SURROUNDED BY LAND"
            # and these are surrounded by ice; 300132301, its parent, is "depressions in
            # the earth"; 300008688 ponds is "usually surrounded on all sides by land";
            # 300008835 glaciers is the ice itself, not water on it; 300008832 glacial
            # landforms is a landform, and a lake is not one. Checked against the live
            # types index 2 Sep — this is a genuine vocabulary gap, not a lookup failure.
            #
            # 300387086 is the one concept that fits on its own terms — "lakes that appear
            # at intervals, generally with predictable cycles" is exactly a supraglacial
            # lake draining through an englacial conduit and refilling. 300008680 is kept
            # as the baseline so these sit in the same hierarchy as the other 71 records,
            # and the precise term the vocabulary cannot express rides in sourceLabels,
            # which is what sourceLabels is for.
            "types": [
                {"identifier": AAT_LAKES[0], "label": AAT_LAKES[1],
                 "sourceLabels": [{"label": sf["feature_type"]}]},
                {"identifier": AAT_INTERMITTENT[0], "label": AAT_INTERMITTENT[1]},
            ],
            "geometry": None,
            "links": [{"type": "seeAlso", "identifier": DOI},
                      {"type": "seeAlso", "identifier": PROJECT_PAGE}],
        }
        if when:
            feat["when"] = when
        if evs:
            shared = [e for e in evs if len((e.get("feature_ids") or "").split(";")) > 1]
            txt = (f"Ephemeral supraglacial lake on the {sf['parent_glacier']}, "
                   f"study-local lake {local} of {source_label(src, sf['source_id'])}. "
                   f"{len(evs)} documented drainage event(s) between {dates[0]} and {dates[-1]}")
            if shared:
                txt += (f", of which {len(shared)} drained simultaneously with other lakes "
                        f"on the same glacier through a shared englacial conduit network")
            feat["descriptions"] = [{"value": txt + ".", "lang": "en", "source": DOI}]
        feats.append(feat)

        rows.append({
            "kgld_id": fid, "title": coined, "alt_names": label,
            "oblast": "", "district": "", "ccode": "KG",
            "latitude": "", "longitude": "", "geometry_captured": "",
            "observation_years": ",".join(years),
            "aat_type": f"{AAT_LAKES[0]};{AAT_INTERMITTENT[0]}",
            "kgld_origin": sf["feature_type"], "mchs_catalog_id": "",
            "basin": "", "mountain_range": sf["parent_glacier"],
            "record_status": sf["identity_status"],
            "close_match": "", "ramsar_ris": "",
            "source_note": feat.get("descriptions", [{}])[0].get("value", ""),
        })
    return feats, rows


def build(data):
    src = {s["source_id"]: s for s in data["sources"]}
    names_by_lake = defaultdict(list)
    for n in data["names"]:
        names_by_lake[n["lake_id"]].append(n)
    class_by_lake = defaultdict(list)
    for c in data["classifications"]:
        class_by_lake[c["lake_id"]].append(c)

    features, rows = [], []
    for lk in data["lakes"]:
        lid = lk["lake_id"]
        if lid in EXCLUDE:
            continue

        # ── names ────────────────────────────────────────────────────────────────────
        names = []
        seen = set()
        for n in names_by_lake[lid]:
            top = (n["name"] or "").strip()
            if not top or top in seen:
                continue
            seen.add(top)
            entry = {"toponym": top}
            if n.get("language_iso639_1"):
                entry["lang"] = n["language_iso639_1"]
            label = source_label(src, (n.get("source_id") or "").strip())
            if label:
                entry["citations"] = [{"label": label}]
            names.append(entry)
        title = (lk["canonical_name"] or "").strip()
        if title and title not in seen:
            names.insert(0, {"toponym": title, "lang": "en"})

        # ── types ────────────────────────────────────────────────────────────────────
        # `label` is the AAT term for `identifier` and nothing else. KGLD's own vocabulary
        # (moraine_glacial, riegel, …) is richer than AAT can express, so it is preserved
        # verbatim in sourceLabels[] rather than dressed up as an AAT label it is not.
        source_labels, intermittent = [], False
        for c in class_by_lake[lid]:
            prop, val = c["property"], (c["value"] or "").strip()
            if prop in ("origin", "mchs_lake_type") and val:
                source_labels.append({"label": val})
            if (prop, val) in INTERMITTENT_MARKERS:
                intermittent = True
                source_labels.append({"label": val})
        ident, label = AAT_LAKES
        typ = {"identifier": ident, "label": label}
        if source_labels:
            seen_sl = set()
            typ["sourceLabels"] = [x for x in source_labels
                                   if not (x["label"] in seen_sl or seen_sl.add(x["label"]))]
        types = [typ]
        if intermittent:
            types.append({"identifier": AAT_INTERMITTENT[0], "label": AAT_INTERMITTENT[1]})

        # ── geometry (8 of 73; the rest are the point of the exercise) ────────────────
        geometry = None
        lat, lon = (lk["latitude"] or "").strip(), (lk["longitude"] or "").strip()
        if lat and lon:
            geometry = {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]}
            cite = source_label(src, (lk.get("coordinate_source_id") or "").strip())
            if cite:
                geometry["citations"] = [{"label": cite}]
            prec_m = int(lk.get("coordinate_precision_m") or 0)
            geometry["certainty"] = "less-certain" if prec_m >= 1000 else "certain"
            # KGLD records `coordinate_precision_m` for every coordinate it publishes, and
            # its methodology §12 requires the figure be retained. LPF's `approximation`
            # takes a tolerance in KILOMETRES, so the metres divide straight into it — the
            # source's own number, not a bucket. This is the slot place#229 opened; before
            # it, the precision survived only as the coarse `certainty` above.
            if prec_m:
                geometry["approximation"] = {"type": "geo:hasSpatialAccuracy",
                                             "tolerance": prec_m / 1000}

        props = {"title": title, "ccodes": ["KG"], "kgld_id": lid}
        for k in ("admin1", "admin2", "basin_name", "mountain_system", "mountain_range",
                  "record_status", "coordinate_method", "coordinate_precision_m"):
            v = (lk.get(k) or "").strip()
            if v:
                props[k] = v
        catalog = next((c["value"] for c in class_by_lake[lid]
                        if c["property"] == "government_catalog_id"), None)
        if catalog:
            props["mchs_catalog_id"] = catalog
        obs_years = observation_years(lid, data["measurements"], data["observations"])
        if obs_years:
            props["observation_years"] = ",".join(obs_years)

        feat = {
            "@id": f"{DOI}#{lid}",
            "type": "Feature",
            "properties": props,
            "names": names,
            "types": types,
            "geometry": geometry,
            "links": [{"type": "seeAlso", "identifier": DOI},
                      {"type": "seeAlso", "identifier": PROJECT_PAGE}],
        }
        ramsar = next((c["value"] for c in class_by_lake[lid]
                       if c["property"] == "ramsar_site_id"), None)
        if ramsar:
            feat["properties"]["ramsar_site_id"] = ramsar
            feat["links"].append({
                "type": "seeAlso",
                "identifier": f"{RAMSAR_RIS}{ramsar}",
                "citations": [{"label": "Ramsar Site; the site boundary may extend beyond "
                                        "the lake, so this is not an identity claim"}],
            })
        for ident, basis in HARD_LINKS.get(lid, []):
            feat["links"].insert(0, {
                "type": "closeMatch",
                "identifier": ident,
                "certainty": "less-certain",
                "citations": [{"label": basis}],
            })

        when = build_when(lid, data["events"], data["observations"])
        if when:
            feat["when"] = when
        desc = morphometry_sentence(lid, data["reference_public"], src)
        if desc:
            feat["descriptions"] = [{"value": desc, "lang": "en", "source": DOI}]
        features.append(feat)

        # ── flat row for the MyD importer ────────────────────────────────────────────
        alts = [n["toponym"] for n in names[1:]]
        rows.append({
            "kgld_id": lid,
            "title": title,
            "alt_names": ";".join(alts),
            "oblast": lk.get("admin1", ""),
            "district": lk.get("admin2", ""),
            "ccode": "KG",
            "latitude": lat,
            "longitude": lon,
            "geometry_captured": "",     # ← place#220 role; contributor fills this in
            "observation_years": ",".join(obs_years),   # candidate values for the above
            "aat_type": ";".join(t["identifier"] for t in types),
            "kgld_origin": "; ".join(s["label"] for s in source_labels),
            "mchs_catalog_id": catalog or "",
            "basin": lk.get("basin_name", ""),
            "mountain_range": lk.get("mountain_range", ""),
            "record_status": lk.get("record_status", ""),
            "close_match": ";".join(i for i, _ in HARD_LINKS.get(lid, [])),
            "ramsar_ris": f"{RAMSAR_RIS}{catalog_ramsar}" if (catalog_ramsar := ramsar) else "",
            "source_note": desc or "",
        })

    sf_feats, sf_rows = build_study_features(data, src)
    features += sf_feats
    rows += sf_rows

    indexing, citation, formatted = citation_blocks()
    # Key order matters: `indexing` and `citation` must precede `features` so the server's
    # streaming reader finds them without parsing the whole array.
    fc = {
        "@context": "https://raw.githubusercontent.com/LinkedPasts/linked-places/master/linkedplaces-context-v1.1.jsonld",
        "type": "FeatureCollection",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "indexing": indexing,
        "citation": citation,
        "features": features,
    }
    return fc, rows, formatted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true",
                    help="validate against whg3's own lpf_v2.0.jsonld (needs jsonschema)")
    args = ap.parse_args()

    data = load()
    fc, rows, formatted = build(data)

    lpf_path = DIR / f"kgld_v{VERSION}.lpf.json"
    lpf_path.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")

    csv_path = DIR / f"kgld_v{VERSION}_myd.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    located = sum(1 for f in fc["features"] if f["geometry"])
    dated = sum(1 for f in fc["features"] if "when" in f)
    sfn = sum(1 for f in fc["features"] if f["properties"]["kgld_id"].startswith("KGLD-F"))
    print(f"features   : {len(fc['features'])} (excluded {len(EXCLUDE)}) "
          f"= {len(fc['features']) - sfn} lakes + {sfn} supraglacial features")
    print(f"  located  : {located}")
    print(f"  dated    : {dated}  (feature-level `when`, evidence-backed only)")
    print(f"  described: {sum(1 for f in fc['features'] if 'descriptions' in f)}")
    print(f"  toponyms : {sum(len(f['names']) for f in fc['features'])}")
    cm = sum(1 for f in fc["features"] for l in f["links"] if l["type"] == "closeMatch")
    print(f"  links    : {cm} closeMatch (pre-existing reconciliations), "
          f"{sum(1 for f in fc['features'] for l in f['links'] if 'ramsar' in l['identifier'])} Ramsar seeAlso")
    print(f"  obs years: {sum(1 for f in fc['features'] if 'observation_years' in f['properties'])}"
          f" features carry candidate capture years")
    print(f"  typed    : {sum(1 for f in fc['features'] if f['types'])} "
          f"(all aat:300008680; {sum(1 for f in fc['features'] if len(f['types']) > 1)} also intermittent)")
    print(f"wrote {lpf_path.name} ({lpf_path.stat().st_size:,} bytes)")
    print(f"wrote {csv_path.name} ({csv_path.stat().st_size:,} bytes)")
    print(f"\ncitation : {formatted}")
    print(f"blocks   : indexing (schema.org, read by WHG ingest) + citation (CSL-JSON)")

    if args.validate:
        import jsonschema
        from referencing import Registry, Resource

        whg3 = Path("/home/stephen/Documents/GitHub/whg3/validation/static")
        schema = json.loads((whg3 / "lpf_v2.0.jsonld").read_text())
        # lpf_v2.0.jsonld $refs csl-citation.json by its absolute $id, and
        # https://whgazetteer.org/schema/csl-citation.json does NOT serve (403 — the copy
        # is published at /static/, not /schema/). Register the local file under its $id,
        # exactly as the browser does in recon-validate.js, or the top-level `citation`
        # block makes validation raise Unresolvable instead of returning errors.
        # (Server-side this never arises: validation/tasks.py validates FEATURE batches,
        # so it never dereferences a top-level ref.)
        csl = json.loads((whg3 / "csl-citation.json").read_text())
        registry = Registry().with_resource(csl["$id"], Resource.from_contents(csl))
        v = jsonschema.Draft7Validator(schema, registry=registry)
        errs = sorted(v.iter_errors(fc), key=lambda e: list(e.absolute_path))

        # ── Expect a FAIL here, and expect it to be exactly the undated features. ──────
        # place#221 (live 2 Sep) tightened the schema's temporal `anyOf`, which until then
        # was vacuously satisfiable: a feature merely LACKING `relations` passed that
        # branch for free. 66 of our 71 lakes carry no `when` because they are permanent
        # bodies of water that no source dates, so they now fail — uniformly and
        # explicably, where before they passed by accident and would have failed the
        # moment a container column was reconciled.
        #
        # This is the file behaving correctly against a stricter schema, NOT a regression.
        # The honest route out is a geometry-level capture date (place#220), which is what
        # the contributor supplies when he places each lake — see README-lpf.md.
        undated = {f["properties"]["kgld_id"] for f in fc["features"] if "when" not in f}
        bad = set()
        for e in errs:
            path = list(e.absolute_path)
            if len(path) >= 2 and path[0] == "features":
                bad.add(fc["features"][path[1]]["properties"]["kgld_id"])
        if errs and bad == undated:
            print(f"\nschema: {len(errs)} error(s) — EXPECTED, and exactly the "
                  f"{len(undated)} undated features (place#221).")
            print("        Not a regression: these lakes carry no `when` because no source")
            print("        dates them. The route out is a geometry-level capture date")
            print("        (place#220), supplied when the contributor places each lake.")
        elif not errs:
            print("\nschema: PASS")
            print("        ⚠️ Unexpected — since place#221 the 66 undated features SHOULD")
            print("        fail. A pass means the schema on disk predates that fix.")
        else:
            print(f"\nschema: {len(errs)} error(s) — UNEXPECTED SHAPE, investigate.")
            print(f"        failing ids not explained by undatedness: "
                  f"{sorted(bad - undated)[:10]}")
            for e in errs[:10]:
                print("  ", "/".join(str(x) for x in e.absolute_path), "→", e.message[:140])


if __name__ == "__main__":
    main()
