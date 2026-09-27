"""Great Britain Historical Database (Vision of Britain) <-> PLATO place-centric JSON.

    python3 vob2plato.py forward <vob dir> out.json
    python3 vob2plato.py inverse <roundtripped .json|.jsonl> <out dir>
    python3 vob2plato.py compare <vob dir> <out dir>

Sources, both CC BY-SA 4.0, open, no registration (downloaded 2026-09-27; see CHECKSUMS.txt):
  SN 9033  Digital Boundaries for Registration Counties of England and Wales, 1851-1911
           doi:10.5255/UKDA-SN-9033-1 - 7 shapefiles, 55 units x 7 census years
  SN 4559  Census Data: Occupational Statistics, 1841-1991
           doi:10.5255/UKDA-SN-4559-2 - 27 tab-separated tables

Coverage is by SHAPE, not by table: every structural form in the deposit is exercised, so that
what the round trip proves is proved for each form, rather than 27 times for three forms.

  boundaries      SN 9033, all 7 years: one entity per G_UNIT, one geometry attestation per year
  statistics long SN 4559 occ_1851_ew: one row per (county, occupation, sex), measure in `persons`
  statistics wide SN 4559 occ_1911_sm_m: one row per district, occupation categories as COLUMNS,
                  with `total`, `retired` and `occupied` as sibling denominators

The wide form is the one that matters. Its column headers are opaque codes ('ix1coal'), their
meaning lives in a PDF, and the denominator a figure should be read against is a sibling cell in
the same row. PLATO can hold both numbers. Whether it can say that one is the universe of the
other is the question this conversion is here to answer.
"""
import csv, glob, json, os, re, sys
import shapefile

VOB = "https://www.visionofbritain.org.uk/unit/"
NS = "https://www.visionofbritain.org.uk/plato/"
PLATO = "https://w3id.org/plato#"
GEOM_SRC = NS + "source/sn9033"
STAT_SRC = NS + "source/sn4559"

# The place-centric profile has no top-level `sources`: a citation carries the source inline or by
# URI. Inlined here so the bibliographic string travels with every claim that rests on it.
SOURCES = {
    GEOM_SRC: {"@id": GEOM_SRC, "title": "GBHD: Digital Boundaries for Registration Counties of England and Wales, 1851-1911 (SN 9033)", "authorityType": "source", "citation":
               "Southall, H.R., Gregory, I., Burton, N. and Aucott, P. (2022) Great Britain Historical "
               "Database: Digital Boundaries for Registration Counties of England and Wales, 1851-1911. "
               "UK Data Service. SN 9033. doi:10.5255/UKDA-SN-9033-1"},
    STAT_SRC: {"@id": STAT_SRC, "title": "GBHD: Census Data: Occupational Statistics, 1841-1991 (SN 4559)", "authorityType": "source", "citation":
               "Gatley, D.A., Woollard, M., Garrett, E. et al. (2022) Great Britain Historical Database: "
               "Census Data: Occupational Statistics, 1841-1991. UK Data Service. SN 4559. "
               "doi:10.5255/UKDA-SN-4559-2"},
}


def cite(src, locator):
    return [{"source": SOURCES[src], "locator": locator}]

# The universes, read off each table's own documentation PDF title. They are recorded here
# because there is nowhere in the source data to read them from: that is a finding, not a defect
# of this script. occ_1851_ew_doc.pdf: "1851 occupational statistics for registration counties in
# England and Wales." occ_1911_sm_m_doc.pdf: "1911 Male Occupational Statistics for towns over
# 5,000 population".
UNIVERSE = {
    "occ_1851_ew": "Persons enumerated in registration counties of England and Wales, 1851",
    "occ_1911_sm_m": "Males in towns over 5,000 population, England and Wales, 1911",
}

YEARS = ["1851", "1861", "1871", "1881", "1891", "1901", "1911"]


def wkt(shape):
    """A shapefile polygon as WKT. Parts are rings; shapefiles do not say which are holes."""
    pts, parts = shape.points, list(shape.parts) + [len(shape.points)]
    rings = []
    for i in range(len(parts) - 1):
        ring = pts[parts[i]:parts[i + 1]]
        if ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        rings.append("(" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in ring) + ")")
    return ("POLYGON " if len(rings) == 1 else "MULTIPOLYGON ") + (
        "(" + ", ".join(rings) + ")" if len(rings) == 1 else "(" + ", ".join("(" + r + ")" for r in rings) + ")")


def rows(path):
    with open(path, newline="", encoding="latin-1") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            yield {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items() if k is not None}


def forward(src, out):
    ents, order = {}, []

    def ent(uid, label=None):
        if uid not in ents:
            ents[uid] = {"@id": VOB + str(uid), "label": label or str(uid), "attestations": []}
            order.append(uid)
        return ents[uid]

    # --- SN 9033: one geometry, name, type and language attestation per unit per census year ---
    ngeom = 0
    for path in sorted(glob.glob(os.path.join(src, "UKDA-9033-xml/xml/*/*.shp"))):
        year = re.search(r"ew(\d{4})", os.path.basename(path)).group(1)
        r = shapefile.Reader(path)
        flds = [f[0] for f in r.fields[1:]]
        for i, (rec, shp) in enumerate(zip(r.records(), r.shapes())):
            d = dict(zip(flds, list(rec)))
            uid = int(d["G_UNIT"])
            e = ent(uid, d["G_NAME"])
            b = f"{NS}att/9033/{year}/{uid}/"
            span = {"startEarliest": f"{year}-01-01", "endLatest": f"{year}-12-31", "label": year}
            # The boundary as digitised for this census year.
            e["attestations"].append({
                "@id": b + "geom",
                "geometries": [{"wkt": wkt(shp)}],
                "timespans": [span],
                "citations": cite(GEOM_SRC, os.path.basename(path)),
            })
            ngeom += 1
            # The unit's name, with its status and language as the deposit records them.
            e["attestations"].append({
                "@id": b + "name",
                "names": [{"toponym": d["G_NAME"], "language": d["G_LANGUAGE"], "sourceLabel": d["G_NAME"]}],
                "formStatus": PLATO + ("Preferred" if d["NAMESTATUS"] == "P" else "Attested"),
                "timespans": [span],
                "citations": cite(GEOM_SRC, os.path.basename(path)),
            })
            e["attestations"].append({
                "@id": b + "type",
                "types": [{"label": d["UNITTYPE"], "sourceLabel": d["UNITTYPE"]}],
                "timespans": [span],
                "citations": cite(GEOM_SRC, os.path.basename(path)),
            })
            # IM_AUTH is the immediate authority: who asserted this row, as against who ultimately
            # observed it. PLATO has derived_from between Sources, but nothing on the claim itself.
            # Parked as a PropertyValue so the round trip can restore the cell. FINDING 3.
            e["attestations"].append({
                "@id": b + "imauth",
                "properties": [{"property": NS + "field/IM_AUTH", "label": "IM_AUTH",
                                "value": d["IM_AUTH"], "sourceLabel": d["IM_AUTH"]}],
                "timespans": [span],
                "citations": cite(GEOM_SRC, os.path.basename(path)),
            })

    # --- SN 4559 occ_1851_ew: the long form. One measure per (county, occupation, sex). ---
    nlong = 0
    for i, r in enumerate(rows(os.path.join(src, "UKDA-4559-tab/tab/occ_1851_ew.tab")), start=2):
        if not r.get("regc_unit"):
            continue
        uid = int(r["regc_unit"])
        e = ent(uid, r["reg_cnty"])
        b = f"{NS}att/4559/occ_1851_ew/{i}/"
        # The dimensional address of the cell. class/subclass/occupation/sex together say WHAT is
        # counted; `persons` says how many. PLATO's PropertyValue holds one value and one unit, so
        # the address has to go somewhere: each dimension becomes its own PropertyValue and the
        # grouping is carried only by the shared attestation @id. FINDING 1.
        props = [{"property": NS + "field/" + k, "label": k, "value": r[k], "sourceLabel": r[k]}
                 for k in ("class_num", "class", "subclass_num", "subclass", "occupation", "sex",
                           "reg_cnty", "reg_num", "rec_num") if r.get(k)]
        props.append({"property": NS + "measure/persons", "label": "persons",
                      "value": int(r["persons"]) if r["persons"] else None, "sourceLabel": r["persons"]})
        e["attestations"].append({
            "@id": b + "cell",
            "properties": props,
            "timespans": [{"startEarliest": "1851-01-01", "endLatest": "1851-12-31", "label": "1851"}],
            # The universe has no home. Recorded in notes so a reader can see it at all. FINDING 2.
            "notes": UNIVERSE["occ_1851_ew"],
            "citations": cite(STAT_SRC, f"occ_1851_ew.tab row {i}"),
        })
        nlong += 1

    # --- SN 4559 occ_1911_sm_m: the wide form. Occupation codes as columns, denominators beside. ---
    nwide = 0
    wide_path = os.path.join(src, "UKDA-4559-tab/tab/occ_1911_sm_m.tab")
    with open(wide_path, newline="", encoding="latin-1") as f:
        wide_cols = [c.strip() for c in f.readline().rstrip("\r\n").split("\t")]
    KEYS = {"page_number", "adm_cnty", "lg_area", "lg_type", "admc_unit", "g_unit"}
    DENOM = {"total", "retired", "occupied"}
    for i, r in enumerate(rows(wide_path), start=2):
        if not r.get("g_unit"):
            continue
        uid = int(r["g_unit"])
        e = ent(uid, r.get("lg_area") or str(uid))
        b = f"{NS}att/4559/occ_1911_sm_m/{i}/"
        props = [{"property": NS + "field/" + k, "label": k, "value": r[k], "sourceLabel": r[k]}
                 for k in wide_cols if k in KEYS and r.get(k)]
        for k in wide_cols:
            if k in KEYS or not r.get(k):
                continue
            # Every measure column is a count of males in this district falling in an occupation
            # category whose meaning is in occ_1911_sm_m_doc.pdf and whose code is opaque. The
            # denominators `total`, `retired` and `occupied` sit in the same row as ordinary
            # siblings: nothing in PLATO can say that `ix1coal` is to be read against `occupied`.
            # FINDING 2 again, and more sharply, because here the universe IS in the data.
            props.append({"property": NS + ("denominator/" if k in DENOM else "measure/") + k,
                          "label": k, "value": int(r[k]) if r[k].lstrip("-").isdigit() else r[k],
                          "sourceLabel": r[k]})
        e["attestations"].append({
            "@id": b + "row",
            "properties": props,
            "timespans": [{"startEarliest": "1911-01-01", "endLatest": "1911-12-31", "label": "1911"}],
            "notes": UNIVERSE["occ_1911_sm_m"],
            "citations": cite(STAT_SRC, f"occ_1911_sm_m.tab row {i}"),
        })
        nwide += 1

    ses = [ents[u] for u in order]
    doc = {
        "$schema": "https://w3id.org/plato/schemas/place-centric.schema.json",
        "profile": "place-centric",
        "gazetteer": {
            "@id": NS,
            "title": "Great Britain Historical Database (prototype conversion of SN 9033 and SN 4559)",
            "licence": "https://creativecommons.org/licenses/by-sa/4.0/",
        },
        "spatialEntities": ses,
    }
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    natt = sum(len(e["attestations"]) for e in ses)
    print(f"{len(ses)} spatial entities, {natt} attestations "
          f"({ngeom} boundary years, {nlong} long-form cells, {nwide} wide-form rows) -> {out}")


def load(path):
    """A place-centric JSON document, or JSON Lines with a header line."""
    if path.endswith(".jsonl"):
        ses = []
        for line in open(path, encoding="utf-8"):
            o = json.loads(line)
            if "spatialEntities" in o:
                ses.extend(o["spatialEntities"])
            elif o.get("@id", "").startswith(VOB) or "attestations" in o:
                ses.append(o)
        return ses
    d = json.load(open(path, encoding="utf-8"))
    return d["spatialEntities"]


def inverse(rt, outdir):
    """Rebuild the source cells from PLATO, so that a diff can fail."""
    os.makedirs(outdir, exist_ok=True)
    shp_rows, long_rows, wide_rows = {}, {}, {}
    for e in load(rt):
        uid = e["@id"].rsplit("/", 1)[-1]
        by_id = {}
        for a in e.get("attestations", []):
            by_id[a["@id"]] = a
        for aid, a in by_id.items():
            m = re.search(r"/att/9033/(\d{4})/(\d+)/(\w+)$", aid)
            if m:
                year, u, kind = m.group(1), m.group(2), m.group(3)
                row = shp_rows.setdefault((year, u), {"G_UNIT": u})
                if kind == "name":
                    n = a["names"][0]
                    row["G_NAME"] = n["toponym"]
                    row["G_LANGUAGE"] = n.get("language", "")
                    row["NAMESTATUS"] = "P" if a.get("formStatus", "").endswith("Preferred") else ""
                elif kind == "type":
                    row["UNITTYPE"] = a["types"][0]["label"]
                elif kind == "imauth":
                    row["IM_AUTH"] = a["properties"][0]["value"]
                continue
            m = re.search(r"/att/4559/(occ_1851_ew|occ_1911_sm_m)/(\d+)/", aid)
            if m:
                tbl, i = m.group(1), int(m.group(2))
                cells = {}
                for p in a.get("properties", []):
                    lab, val = p.get("sourceLabel"), p.get("value")
                    # The parsed number and the source's own cell text are both carried. Read the
                    # text, but require the number to agree with it, so that corrupting either is
                    # caught rather than masked by the other.
                    if val is not None and lab is not None and str(val) != lab:
                        lab = f"<value {val!r} disagrees with sourceLabel {lab!r}>"
                    cells[p["label"]] = lab if lab is not None else val
                # The unit id is not a property: it is the entity, and the attestation hangs off
                # it. Recovered from the @id rather than duplicated into the record, which would
                # make the round trip pass by carrying the answer twice.
                cells.setdefault("regc_unit" if tbl == "occ_1851_ew" else "g_unit", uid)
                (long_rows if tbl == "occ_1851_ew" else wide_rows)[i] = cells
    with open(os.path.join(outdir, "boundaries.tsv"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["year", "G_UNIT", "G_NAME", "NAMESTATUS", "G_LANGUAGE", "UNITTYPE", "IM_AUTH"])
        for (year, u), r in sorted(shp_rows.items()):
            w.writerow([year, u, r.get("G_NAME", ""), r.get("NAMESTATUS", ""), r.get("G_LANGUAGE", ""),
                        r.get("UNITTYPE", ""), r.get("IM_AUTH", "")])
    for name, d in (("occ_1851_ew", long_rows), ("occ_1911_sm_m", wide_rows)):
        cols, seen = [], set()
        for i in sorted(d):
            for k in d[i]:
                if k not in seen:
                    seen.add(k); cols.append(k)
        with open(os.path.join(outdir, name + ".tsv"), "w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["__row"] + cols)
            for i in sorted(d):
                w.writerow([i] + [d[i].get(c, "") for c in cols])
    print(f"rebuilt {len(shp_rows)} boundary rows, {len(long_rows)} long cells, {len(wide_rows)} wide rows "
          f"-> {outdir}")


def compare(src, rebuilt):
    """Diff the rebuilt cells against the source's own cells. Non-empty cells only."""
    diffs, checked = [], 0

    got = {}
    for r in rows(os.path.join(rebuilt, "boundaries.tsv")):
        got[(r["year"], r["G_UNIT"])] = r
    for path in sorted(glob.glob(os.path.join(src, "UKDA-9033-xml/xml/*/*.shp"))):
        year = re.search(r"ew(\d{4})", os.path.basename(path)).group(1)
        rd = shapefile.Reader(path)
        flds = [f[0] for f in rd.fields[1:]]
        for rec in rd.records():
            d = dict(zip(flds, list(rec)))
            key = (year, str(int(d["G_UNIT"])))
            g = got.get(key)
            for col in ("G_NAME", "NAMESTATUS", "G_LANGUAGE", "UNITTYPE", "IM_AUTH"):
                want = str(d[col]).strip()
                if not want:
                    continue
                checked += 1
                have = (g or {}).get(col, "")
                if have != want:
                    diffs.append(f"9033 {year} {key[1]} {col}: source {want!r} != rebuilt {have!r}")

    for name, keycol in (("occ_1851_ew", "regc_unit"), ("occ_1911_sm_m", "g_unit")):
        got = {int(r["__row"]): r for r in rows(os.path.join(rebuilt, name + ".tsv"))}
        for i, r in enumerate(rows(os.path.join(src, f"UKDA-4559-tab/tab/{name}.tab")), start=2):
            if not r.get(keycol):
                continue
            g = got.get(i, {})
            for col, want in r.items():
                if not want:
                    continue
                checked += 1
                have = g.get(col, "")
                if have != want:
                    diffs.append(f"{name} row {i} {col}: source {want!r} != rebuilt {have!r}")

    print(f"{checked} non-empty cells checked, {len(diffs)} differences")
    for d in diffs[:20]:
        print("  ", d)
    return 1 if diffs else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "forward":
        forward(sys.argv[2], sys.argv[3])
    elif cmd == "inverse":
        inverse(sys.argv[2], sys.argv[3])
    elif cmd == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    else:
        sys.exit("forward | inverse | compare")
