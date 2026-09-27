"""Necessary Reunions place CSVs <-> PLATO place-centric JSON, and a round-trip check.

    python3 neru2plato.py forward  <neru_csv_dir> out.json
    python3 neru2plato.py inverse  in.json <out_dir>
    python3 neru2plato.py compare  <neru_csv_dir> <out_dir>

Every non-derived cell is carried. Attestation @ids encode table + source row, so the inverse
knows where each fact came from and in what order, including attestations with no facet.
Allowed normalisation: cells are compared after str.strip(). Out of scope, as derived or
spreadsheet artefacts: blank padding rows, TEST_* (formula errors), PLACE / RELATED_PLACE
(lookup display columns), the unnamed status column (true iff the row has content),
and CHECKED? (FALSE on every row).
"""
import csv, json, sys, uuid, re, collections
from pathlib import Path

NS = "https://id.necessaryreunions.org/"
V = NS + "vocab/"
PLATO = "https://w3id.org/plato#"
FILES = {"places": "places", "alt": "altLabels", "types": "placeTypes", "rel": "placeRelation"}
EXT = {  # identity columns -> URI namespace (verbatim URIs pass through)
    "AMH_ID": NS + "ext/amh/", "EXISTING_ID": NS + "ext/globalise/", "EXTERNAL_IDS": NS + "ext/esta/",
}
# The NeRu vocabulary a real conversion would publish. The inverse requires a relation's wording
# (relationLabel, plato:source_label) and its type to agree.
RELATION_LABELS = {V + "relation/part-of": "Part Of"}
LEVEL = {"certain": PLATO + "Certain", "uncertain": PLATO + "Uncertain"}
WORD = {v: k for k, v in LEVEL.items()}
LAT = "http://www.w3.org/2003/01/geo/wgs84_pos#lat"
# NeRu's CCODES holds country names; PLATO's ccodes holds ISO 3166-1 alpha-2 codes.
COUNTRY = {"India": "IN"}
NAME_OF = {v: k for k, v in COUNTRY.items()}


TITLE = "Necessary Reunions places (prototype conversion)"
PROPS = {LAT: "latitude", V + "workflowFlag": "workflow flag"}


def same_num(a, b):
    """Equal to 15 significant digits: what every decimal -> double -> decimal round trip keeps."""
    return f"{float(a):.15g}" == f"{float(b):.15g}"


def read(d, key):
    p = Path(d) / f"Places Form - Meenu - {FILES[key]}.csv"
    return list(csv.DictReader(open(p, encoding="utf-8-sig")))


def se_uri(glob_id):
    return NS + "place/" + str(uuid.uuid5(uuid.NAMESPACE_DNS, glob_id))


def cite(src, page):
    if not src.strip() and not page.strip():
        return {}
    # A locator with no source is a defect in the data (2 cells); keep it, with an empty title.
    c = {"source": {"title": src.strip()}}
    if page.strip():
        c["locator"] = page.strip()
    return {"citations": [c]}


def remark(aid, text, src, page, target):
    """A remark with its own source: a meta-attestation on the attestation it comments on."""
    a = {"@id": aid, **({"notes": text.strip()} if text.strip() else {}), "meta": {"targetAttestation": target, "metaType": PLATO + "Annotates"}}
    a.update(cite(src, page))
    return a


def forward(d, out):
    P, A, T, R = (read(d, k) for k in ("places", "alt", "types", "rel"))
    ents, order = {}, []

    def ent(g):
        if g not in ents:
            ents[g] = {"@id": se_uri(g), "label": g, "entityIdentifier": g, "namespace": "neru", "attestations": [], "identityRelations": []}
            order.append(g)
        return ents[g]

    for i, r in enumerate(P, start=2):  # spreadsheet row numbers
        g = r["GLOB_ID"].strip()
        if not g:
            continue
        e = ent(g)
        e["label"] = r["PREF_LABEL"].strip()
        base = f"{NS}attestation/places/{i}/"
        if r["CCODES"].strip():
            e["ccodes"] = [COUNTRY[r["CCODES"].strip()]]
        # the preferred label: every place row has one, so this attestation also locates the row
        a = {"@id": base + "pref", "names": [{"toponym": r["PREF_LABEL"].strip()}], "formStatus": PLATO + "Preferred"}
        a.update(cite(r["PREF_LABEL_SOURCE"], r["PREF_LABEL_SOURCE_PAGE"]))
        e["attestations"].append(a)
        if r["PREF_LABEL_REMARKS"].strip():
            e["attestations"].append(remark(base + "pref-remark", r["PREF_LABEL_REMARKS"], r["PREF_LABEL_REMARKS_SOURCE"], r["PREF_LABEL_REMARKS_SOURCE_PAGE"], base + "pref"))
        # coordinates: the text verbatim in the WKT, the numbers in reprPoint
        if r["LATITUDE"].strip():
            lat, lon = r["LATITUDE"].strip(), r["LONGITUDE"].strip()
            c = r["COORD_CERTAINTY"]
            q = {"certaintyLevel": LEVEL[c.strip()]} if c.strip() else None
            if lon:
                geom = {"wkt": f"POINT ({lon} {lat})", "reprPoint": [float(lon), float(lat)]}
                if q:
                    geom["qualification"] = q
                a = {"@id": base + "coord", "geometries": [geom]}
            else:  # a latitude with no longitude is not a location
                pv = {"property": LAT, "label": "latitude", "value": float(lat), "sourceLabel": lat}
                if q:
                    pv["qualification"] = q
                a = {"@id": base + "coord", "properties": [pv]}
            a.update(cite(r["COORD_SOURCE"], r["COORD_SOURCE_PAGE"]))
            e["attestations"].append(a)
            if r["COORD_REMARKS"].strip():
                e["attestations"].append(remark(base + "coord-remark", r["COORD_REMARKS"], r["COORD_REMARKS_SOURCE"], r["COORD_REMARKS_SOURCE_PAGE"], base + "coord"))
        # overall remarks: an attestation with no facet
        if r["OVERALL_REMARKS"].strip() or r["OVERALL_SOURCE"].strip():
            a = {"@id": base + "overall"}
            if r["OVERALL_REMARKS"].strip():
                a["notes"] = r["OVERALL_REMARKS"].strip()
            a.update(cite(r["OVERALL_SOURCE"], r["OVERALL_SOURCE_PAGE"]))
            e["attestations"].append(a)
        # identity links (NeRu does not state their strength)
        for col in ("GEONAMES_ID", "WIKIDATA_ID", "WHG_ID", "TGN_ID", "AMH_ID", "EXISTING_ID", "EXTERNAL_IDS"):
            val = r[col].strip()
            if not val:
                continue
            obj = val if val.startswith("http") else EXT[col] + val
            ir = {"@id": f"{base}identity/{col}", "subject": e["@id"], "object": obj, "identityType": "unspecified", "assertedBy": NS}
            if col == "EXISTING_ID" and r["EXISTING_PLACE"].strip():
                ir["basis"] = r["EXISTING_PLACE"].strip()
            e["identityRelations"].append(ir)

    for i, r in enumerate(A, start=2):
        g = r["GLOB_ID"].strip()
        if not g:
            continue
        base = f"{NS}attestation/alt/{i}/"
        a = {"@id": base + "name", "names": [{"toponym": r["ALT_LABEL"].strip()}], "formStatus": PLATO + "Attested"}
        a.update(cite(r["ALT_LABEL_SOURCE"], r["ALT_LABEL_SOURCE_PAGE"]))
        if r[""].strip():  # the one 'annotated' flag
            a["properties"] = [{"property": V + "workflowFlag", "label": "workflow flag", "value": r[""].strip()}]
        ent(g)["attestations"].append(a)
        if (r["ALT_LABEL_REMARKS"] + r["ALT_LABEL_REMARKS_SOURCE"] + r["ALT_LABEL_REMARKS_SOURCE_PAGE"]).strip():
            ent(g)["attestations"].append(remark(base + "remark", r["ALT_LABEL_REMARKS"], r["ALT_LABEL_REMARKS_SOURCE"], r["ALT_LABEL_REMARKS_SOURCE_PAGE"], base + "name"))

    for i, r in enumerate(T, start=2):
        g = r["GLOB_ID"].strip()
        if not g or not (r["TYPE"].strip() or r["TYPE_REMARKS"].strip() or r["SOURCE"].strip()):
            continue
        a = {"@id": f"{NS}attestation/types/{i}"}
        if r["TYPE"].strip():
            a["types"] = [{"label": r["TYPE"].strip(), "sourceLabel": r["TYPE"].strip()}]
        if r["TYPE_REMARKS"].strip():
            a["notes"] = r["TYPE_REMARKS"].strip()
        a.update(cite(r["SOURCE"], r["SOURCE_PAGE"]))
        ent(g)["attestations"].append(a)

    for i, r in enumerate(R, start=2):
        g = r["GLOB_ID"].strip()
        if not g or not r["RELATION"].strip():
            continue
        tgt = r["RELATED_GLOB_ID"].strip()
        ent(tgt)  # a referent-only entity if NeRu has no row for it (GLOB_277)
        a = {"@id": f"{NS}attestation/rel/{i}",
             "relations": [{"relatesTo": se_uri(tgt), "relationType": V + "relation/" + r["RELATION"].strip().lower().replace(" ", "-"),
                            "relationLabel": r["RELATION"].strip()}]}
        if r["RELATION_REMARKS"].strip():
            a["notes"] = r["RELATION_REMARKS"].strip()
        a.update(cite(r["SOURCE"], r["SOURCE_PAGE"]))
        ent(g)["attestations"].append(a)

    ses = []
    for g in order:
        e = ents[g]
        if not e["identityRelations"]:
            del e["identityRelations"]
        if not e["attestations"]:
            del e["attestations"]
        ses.append(e)
    doc = {"$schema": "https://w3id.org/plato/schemas/place-centric.schema.json", "profile": "place-centric",
           "gazetteer": {"@id": NS, "title": TITLE},
           "spatialEntities": ses}
    json.dump(doc, open(out, "w"), ensure_ascii=False, indent=1)
    print(f"{len(ses)} spatial entities, {sum(len(e.get('attestations', [])) for e in ses)} attestations, "
          f"{sum(len(e.get('identityRelations', [])) for e in ses)} identity relations -> {out}")


# ---------------------------------------------------------------------------------- inverse
def load_entities(path):
    """Accept the place-centric JSON document or JSON Lines (header, then one entity per line)."""
    txt = open(path).read()
    try:
        doc = json.loads(txt)
        return doc.get("gazetteer", {}), doc["spatialEntities"], doc.get("identityRelations", [])
    except json.JSONDecodeError:
        lines = [json.loads(l) for l in txt.splitlines() if l.strip()]
        head, lines = lines[0].get("gazetteer", {}), lines[1:]
        return head, [l for l in lines if "subject" not in l], [l for l in lines if "subject" in l]


def first(x):
    return x[0] if isinstance(x, list) and x else x


def cit(a):
    c = first(a.get("citations") or [{}]) or {}
    s = c.get("source") or {}
    return (s.get("title", "") if isinstance(s, dict) else s), c.get("locator", "")


def inverse(path, outdir):
    head, ses, loose_ids = load_entities(path)
    rows = collections.defaultdict(dict)  # (table,row) -> dict
    disagree = []  # a value carried twice (text and number, wording and type) must agree
    # ...and a value the conversion asserts without the CSVs holding it must be what was written.
    if head.get("title") != TITLE:
        disagree.append(["gazetteer", head])
    for e in ses:
        if e.get("namespace") != "neru":
            disagree.append([e.get("entityIdentifier"), "namespace", e.get("namespace")])
        pref = [a for a in e.get("attestations") or [] if a["@id"].endswith("/pref")]
        want = pref[0]["names"][0]["toponym"] if pref else e.get("entityIdentifier")
        if e.get("label") != want:
            disagree.append([e.get("entityIdentifier"), "label", e.get("label"), want])
        for a in e.get("attestations") or []:
            if a.get("meta"):
                tail = {"pref-remark": "pref", "coord-remark": "coord", "remark": "name"}[a["@id"].rsplit("/", 1)[1]]
                if a["meta"].get("metaType") != PLATO + "Annotates" or a["meta"].get("targetAttestation") != a["@id"].rsplit("/", 1)[0] + "/" + tail:
                    disagree.append([e.get("entityIdentifier"), "meta", a["meta"]])
            for ty in a.get("types") or []:
                if ty.get("label") != ty.get("sourceLabel"):
                    disagree.append([e.get("entityIdentifier"), "type", ty])
            for pv in a.get("properties") or []:
                if PROPS.get(pv.get("property")) != pv.get("label"):
                    disagree.append([e.get("entityIdentifier"), "property", pv])
    for ir in [ir for e in ses for ir in (e.get("identityRelations") or [])] + loose_ids:
        if ir.get("assertedBy") != NS:
            disagree.append([ir["@id"], "assertedBy", ir.get("assertedBy")])
    glob_of = {e["@id"]: e.get("entityIdentifier") for e in ses}
    idrels = [ir for e in ses for ir in (e.get("identityRelations") or [])] + loose_ids
    for e in ses:
        g = glob_of.get(e["@id"])
        for a in e.get("attestations") or []:
            m = re.match(re.escape(NS) + r"attestation/(\w+)/(\d+)/?(.*)$", a["@id"])
            table, row, part = m.group(1), int(m.group(2)), m.group(3)
            r = rows[(table, row)]
            r["GLOB_ID"] = g
            src, loc = cit(a)
            if table == "places":
                if part == "pref":
                    r.update(PREF_LABEL=a["names"][0]["toponym"], PREF_LABEL_SOURCE=src, PREF_LABEL_SOURCE_PAGE=loc)
                    if e.get("ccodes"):
                        r["CCODES"] = NAME_OF[e["ccodes"][0]]
                elif part == "coord":
                    if a.get("geometries"):
                        gm = first(a["geometries"])
                        wm = re.fullmatch(r"POINT \((\S+) (\S+)\)", gm["wkt"])
                        lon, lat = wm.groups() if wm else ("", "")
                        if not wm or not (same_num(lon, gm["reprPoint"][0]) and same_num(lat, gm["reprPoint"][1])):
                            disagree.append([g, gm["wkt"], gm["reprPoint"]])
                    else:
                        gm = first(a["properties"]); lon, lat = "", gm["sourceLabel"]
                        if not same_num(lat, gm["value"]):
                            disagree.append([g, lat, gm["value"]])
                    lv = (gm.get("qualification") or {}).get("certaintyLevel")
                    r.update(LATITUDE=lat, LONGITUDE=lon, COORD_SOURCE=src, COORD_SOURCE_PAGE=loc,
                             COORD_CERTAINTY=WORD[lv] if lv else "")
                elif part in ("pref-remark", "coord-remark"):
                    k = "PREF_LABEL" if part == "pref-remark" else "COORD"
                    r.update({k + "_REMARKS": a.get("notes", ""), k + "_REMARKS_SOURCE": src, k + "_REMARKS_SOURCE_PAGE": loc})
                elif part == "overall":
                    r.update(OVERALL_REMARKS=a.get("notes", ""), OVERALL_SOURCE=src, OVERALL_SOURCE_PAGE=loc)
            elif table == "alt":
                if part == "name":
                    r.update(ALT_LABEL=a["names"][0]["toponym"], ALT_LABEL_SOURCE=src, ALT_LABEL_SOURCE_PAGE=loc)
                    for p in a.get("properties") or []:
                        r[""] = p["value"]
                else:
                    r.update(ALT_LABEL_REMARKS=a.get("notes", ""), ALT_LABEL_REMARKS_SOURCE=src, ALT_LABEL_REMARKS_SOURCE_PAGE=loc)
            elif table == "types":
                r.update(TYPE=(first(a.get("types")) or {}).get("sourceLabel", ""), TYPE_REMARKS=a.get("notes", ""), SOURCE=src, SOURCE_PAGE=loc)
            elif table == "rel":
                rel = first(a["relations"])
                tgt = next(x for x in ses if x["@id"] == rel["relatesTo"])
                if rel.get("relationLabel") != RELATION_LABELS.get(rel["relationType"]):
                    disagree.append([g, rel.get("relationLabel"), rel["relationType"]])
                r.update(RELATION=RELATION_LABELS.get(rel["relationType"], ""), RELATED_GLOB_ID=glob_of.get(tgt["@id"]),
                         RELATION_REMARKS=a.get("notes", ""), SOURCE=src, SOURCE_PAGE=loc)
    for ir in idrels:
        m = re.match(re.escape(NS) + r"attestation/places/(\d+)/identity/(\w+)$", ir["@id"])
        col = m.group(2)
        val = ir["object"]
        for k, ns in EXT.items():
            if col == k and val.startswith(ns):
                val = val[len(ns):]
        r = rows[("places", int(m.group(1)))]
        r[col] = val
        if ir.get("basis"):
            r["EXISTING_PLACE"] = ir["basis"]
    Path(outdir).mkdir(exist_ok=True)
    for t in FILES:
        recs = sorted((row, r) for (tb, row), r in rows.items() if tb == t)
        json.dump([{"row": row, **r} for row, r in recs], open(Path(outdir) / f"{t}.json", "w"), ensure_ascii=False, indent=0)
    json.dump(disagree, open(Path(outdir) / "disagree.json", "w"), ensure_ascii=False)
    print("inverse:", {t: sum(1 for k in rows if k[0] == t) for t in FILES})


# ---------------------------------------------------------------------------------- compare
SKIP = re.compile(r"^(TEST_.*|PLACE|RELATED_PLACE|CHECKED\?|EXTERNAL_ID)$")


def compare(d, outdir):
    total = diffs = 0
    stripped = 0
    for t in FILES:
        orig = read(d, t)
        back = {r["row"]: r for r in json.load(open(Path(outdir) / f"{t}.json"))}
        for i, r in enumerate(orig, start=2):
            content = {k: v for k, v in r.items() if not SKIP.match(k) and k != "" and v.strip()}
            if t == "alt" and r[""].strip():
                content[""] = r[""]
            if t in ("types", "rel", "places") and "GLOB_ID" in content and len(content) == 1:
                continue  # a row with only an id carries nothing
            if not content:
                if i in back:
                    diffs += 1; print("  spurious row", t, i)
                continue
            b = back.get(i, {})
            for k, v in content.items():
                total += 1
                stripped += v != v.strip()
                if (b.get(k) or "").strip() != v.strip():
                    diffs += 1
                    if diffs <= 15:
                        print(f"  DIFF {t} row {i} {k!r}: {v.strip()!r} != {b.get(k)!r}")
    disagree = json.load(open(Path(outdir) / "disagree.json"))
    for d_ in disagree[:10]:
        print("  CARRIED TWICE, DISAGREES", d_)
    print(f"compared {total} non-empty cells: {diffs} differ; {stripped} had surrounding whitespace (normalised); "
          f"{len(disagree)} values whose two carriers disagree")
    return diffs + len(disagree)


if __name__ == "__main__":
    cmd = sys.argv[1]
    sys.exit({"forward": forward, "inverse": inverse, "compare": compare}[cmd](*sys.argv[2:]) and 1 or 0)
