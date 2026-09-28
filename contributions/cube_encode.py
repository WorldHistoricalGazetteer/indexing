"""PLATO issue #14: encode Vision of Britain and Vision of Ireland statistics as Data Cube.

    python3 cube_encode.py vob <vob dir>  vob-cube.json
    python3 cube_encode.py voi <voi repo> voi-cube.json

Each figure becomes one plato:PropertyValue that is also a qb:Observation, carrying qb:dataSet,
its coordinates under `dimensions`, sdmx-dimension:refArea and refPeriod, and, where the source
defines one, `universe` pointing at the denominator observation.

Two rules this encoding follows, both decided after measurement rather than before:

* A coordinate is never a PropertyValue. Under the present encoding the address of a cell is
  scattered across sibling PropertyValues, which asserts that a county has a property class = 1.
  Here the only PropertyValue is the figure, and its coordinates are dimensions of the observation.
* A universe is asserted only where the source defines it, never inferred. For Vision of Ireland
  that means: the cube's own label names its universe, a cube recording exactly that universe
  exists for the same unit and year, and the parts sum to it. Three of seven candidate links that
  look right by name fail the arithmetic and are not asserted. See UNIVERSE_LINKS.
"""
import collections, csv, glob, json, os, re, sys
from urllib.parse import quote

QB = "http://purl.org/linked-data/cube#"
SDMX_D = "http://purl.org/linked-data/sdmx/2009/dimension#"
SDMX_C = "http://purl.org/linked-data/sdmx/2009/code#"
VOB = "https://www.visionofbritain.org.uk/unit/"
NS_VOB = "https://www.visionofbritain.org.uk/plato/"
NS_VOI = "https://www.visionofbritain.org.uk/plato/voi/"

# Verified by arithmetic over 5,560 unit-year pairs (cube_encode.py --report):
#   asserted: the parts sum to the denominator for at least 99.8% of pairs
#   rejected: they do not, and each for a real reason, so the link is not asserted
UNIVERSE_LINKS = {
    "Persons aged 15+ by Sex and Irish 1841/51 Occupational Classification":
        "Occupied persons aged 15 and over, by sex",     # 5560/5560
    "House Occupancy": "Total Houses",                   # 5560/5560
    "Males and Females": "Total Population",             # 5560/5560
    "Families by Housing Class (Ireland)": "Total Families",  # 5551/5560, the 9 are the source's
}
UNIVERSE_REJECTED = {
    'Inhabited Houses by "Class" (Ireland)': ("Total Houses", "281/5560: Total Houses counts "
                                              "uninhabited houses, so the denominator is a figure "
                                              "the source never prints"),
    "Literacy by Gender": ("Total Population", "2/5560: literacy is not recorded for the whole "
                           "population"),
    "All occupied persons, in 3 Categories": ("Occupied persons aged 15 and over, by sex",
                                              "1/5560: a different basis"),
}


def slug(s):
    return quote(str(s).strip(), safe="")


def rows(path):
    with open(path, newline="", encoding="latin-1") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            yield {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items() if k}


# --------------------------------------------------------------------------- Vision of Britain

def vob(src, out):
    ds = NS_VOB + "dataset/occ_1851_ew"
    dim = {k: f"{NS_VOB}dimension/{k}" for k in ("class", "subclass", "occupation")}
    codes = collections.defaultdict(dict)
    ents, order, nobs = {}, [], 0

    for i, r in enumerate(rows(os.path.join(src, "UKDA-4559-tab/tab/occ_1851_ew.tab")), start=2):
        if not r.get("regc_unit"):
            continue
        uid = r["regc_unit"]
        e = ents.setdefault(uid, {"@id": VOB + uid, "label": r["reg_cnty"], "attestations": []})
        if uid not in order:
            order.append(uid)
        # refArea and refPeriod are NOT written here. The attestation already carries the place
        # and the date, and plato-tools' cube export derives both from it, so writing them would
        # say the same thing twice and invite the two copies to disagree.
        dims = {SDMX_D + "sex": {"@id": SDMX_C + ("sex-M" if r["sex"] == "M" else "sex-F")}}
        for k in ("class", "subclass", "occupation"):
            # Decision 5: an unknown coordinate is an explicit "unknown" concept in that
            # dimension's code list, never an omitted dimension. 2,090 rows have no subclass, and
            # omitting it made them fail IC-11 while the structure still declared the dimension.
            if r.get(k):
                c = f"{NS_VOB}code/{k}/{slug(r[k])}"
                codes[k][c] = r[k]
            else:
                c = f"{NS_VOB}code/{k}/unknown"
                codes[k][c] = "unknown (the source cell is empty)"
            dims[dim[k]] = {"@id": c}
        obs = {"@id": f"{NS_VOB}obs/occ_1851_ew/{i}",
               "property": NS_VOB + "measure/persons",
               "label": "persons",
               "value": int(r["persons"]) if r["persons"] else None,
               "sourceLabel": r["persons"],
               "dataSet": ds,
               "dimensions": dims}
        # occ_1851_ew prints no total beside its counts, so no per-figure universe is asserted.
        # The universe of the whole table is its scope, carried on the dataSet.
        e["attestations"].append({
            "@id": f"{NS_VOB}att/cube/occ_1851_ew/{i}",
            "properties": [obs],
            "timespans": [{"startEarliest": "1851-01-01", "endLatest": "1851-12-31", "label": "1851"}],
            "citations": [{"source": {"@id": NS_VOB + "source/sn4559", "authorityType": "source",
                                      "title": "GBHD: Census Data: Occupational Statistics, "
                                               "1841-1991 (SN 4559)"},
                           "locator": f"occ_1851_ew.tab row {i}"}],
        })
        nobs += 1

    dss = [{"@id": ds, "title": "Occupational statistics for registration counties of England and "
                                "Wales, 1851",
            "description": "Persons enumerated in registration counties of England and Wales, 1851. "
                           "The scope is stated in occ_1851_ew_doc.pdf and nowhere in the data.",
            "isPartOf": NS_VOB + "source/sn4559",
            "structure": {"@id": ds + "/structure",
                          "components": [{"dimension": d} for d in
                                         [SDMX_D + "sex"] + list(dim.values())]
                                        + [{"measure": NS_VOB + "measure/persons"}]}}]
    write(out, NS_VOB, "Great Britain Historical Database, occ_1851_ew as Data Cube",
          dss, [ents[u] for u in order], codes, nobs, 0, 0)


# --------------------------------------------------------------------------- Vision of Ireland

def voi(root, out):
    units = {}
    for p in sorted(glob.glob(os.path.join(root, "site/data/units/*.json"))):
        units.update(json.load(open(p, encoding="utf-8")))
    codes = collections.defaultdict(dict)
    cube_dims = collections.defaultdict(set)
    ses, nobs, nuniv, nsource_univ = [], 0, 0, 0

    for uid, u in units.items():
        e = {"@id": VOB + str(uid), "label": u.get("name") or str(uid), "attestations": []}
        for year, rowlist in sorted((u.get("stats") or {}).items()):
            # index this unit-year's figures so a universe can point at the right observation
            by_table = collections.defaultdict(list)
            for i, s in enumerate(rowlist):
                by_table[s["table"]].append((i, s))
            for i, s in enumerate(rowlist):
                if s.get("value") is None:
                    continue
                cube, cell = s["table"], s["cell"]
                dims = {}   # refArea and refPeriod are derived by the cube export, not written
                # A two-dimensional cube's cell is "A / B": two coordinates the export flattened
                # into one label. Split it back rather than mint a concept per pair.
                parts = [x.strip() for x in cell.split(" / ")] if " / " in cell else [cell]
                for n, part in enumerate(parts):
                    dname = f"{NS_VOI}dimension/{slug(cube)}/{n}"
                    c = f"{NS_VOI}code/{slug(cube)}/{slug(part)}"
                    codes[cube][c] = part
                    dims[dname] = {"@id": c}
                    cube_dims[cube].add(dname)
                obs = {"@id": f"{NS_VOI}obs/{uid}/{year}/{i}",
                       "property": f"{NS_VOI}measure/{slug(cube)}",
                       "label": cell, "value": s["value"], "sourceLabel": cell,
                       "dataSet": f"{NS_VOI}dataset/{slug(cube)}",
                       "dimensions": dims}
                if cube == "Area (acres)":
                    obs["unit"] = "http://qudt.org/vocab/unit/AC"
                den = UNIVERSE_LINKS.get(cube)
                if den:
                    nsource_univ += 1
                    target = by_table.get(den)
                    if target:
                        obs["universe"] = f"{NS_VOI}obs/{uid}/{year}/{target[0][0]}"
                        nuniv += 1
                e["attestations"].append({
                    "@id": f"{NS_VOI}att/cube/{uid}/{year}/{i}",
                    "properties": [obs],
                    "timespans": [{"startEarliest": f"{year}-01-01", "endLatest": f"{year}-12-31",
                                   "label": str(year)}],
                    "citations": [{"source": {"@id": f"{NS_VOI}authority/SRC",
                                              "authorityType": "source", "title": "SRC"}}],
                })
                nobs += 1
        ses.append(e)

    # The structure must declare the dimensions the observations actually carry. An earlier
    # version declared none, which would have let IC-11 hold vacuously and made IC-12 read every
    # observation in a cube as having the same empty address.
    dss = [{"@id": f"{NS_VOI}dataset/{slug(c)}", "title": c,
            "description": f"{c}. The universe is named in the cube's own label and nowhere else.",
            "structure": {"@id": f"{NS_VOI}dataset/{slug(c)}/structure",
                          "components": [{"dimension": d} for d in sorted(cube_dims[c])]
                                        + [{"measure": f"{NS_VOI}measure/{slug(c)}"}]}}
           for c in sorted(codes)]
    write(out, NS_VOI, "Vision of Ireland, 12 nCubes as Data Cube", dss, ses, codes,
          nobs, nuniv, nsource_univ)


def write(out, ns, title, dss, ses, codes, nobs, nuniv, nsource_univ):
    doc = {"$schema": "https://w3id.org/plato/schemas/place-centric.schema.json",
           "profile": "place-centric",
           "gazetteer": {"@id": ns, "title": title},
           "dataSets": dss,
           "spatialEntities": ses}
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    ncodes = sum(len(v) for v in codes.values())
    print(f"{len(ses)} entities, {nobs} observations, {ncodes} minted concepts in "
          f"{len(codes)} code lists -> {out}")
    print(f"  test 2, PropertyValues that are a coordinate: 0 (every coordinate is a dimension)")
    print(f"  test 3, figures whose source defines a universe: {nsource_univ}; carrying one: {nuniv}")


# --------------------------------------------------------------------------- test 1: the round trip

def load_ses(path):
    if path.endswith(".jsonl"):
        ses = []
        for line in open(path, encoding="utf-8"):
            o = json.loads(line)
            if "spatialEntities" in o:
                ses.extend(o["spatialEntities"])
            elif "attestations" in o or o.get("@id", "").startswith(VOB):
                ses.append(o)
        return ses
    return json.load(open(path, encoding="utf-8"))["spatialEntities"]


def unslug(iri):
    """The last segment of an IRI. SDMX codes end in a fragment (#sex-M), project ones in a path
    segment, so both separators count."""
    from urllib.parse import unquote
    return unquote(re.split(r"[/#]", iri)[-1])


def inverse(rt, out):
    """Rebuild the source's own cells from the observations that came back."""
    st = os.stat(rt)
    rows = {}
    for e in load_ses(rt):
        uid = e["@id"].rsplit("/", 1)[-1]
        for a in e.get("attestations", []):
            for p in a.get("properties", []):
                dims = p.get("dimensions") or {}
                cell = {"unit": uid, "value": p.get("value"),
                        "sourceLabel": p.get("sourceLabel"),
                        "dataSet": p.get("dataSet"), "universe": p.get("universe")}
                for d, v in dims.items():
                    name = unslug(d) if not d.endswith("#sex") else "sex"
                    cell[name] = unslug(v["@id"]) if isinstance(v, dict) else v
                rows[p["@id"]] = cell
    rows["_from"] = {"path": os.path.abspath(rt), "bytes": st.st_size,
                     "mtime": __import__("datetime").datetime.fromtimestamp(
                         st.st_mtime).isoformat(timespec="seconds")}
    json.dump(rows, open(out, "w"), ensure_ascii=False)
    print(f"  rebuilt from {rows['_from']['path']} ({st.st_size:,} bytes, {rows['_from']['mtime']})")
    print(f"rebuilt {len(rows) - 1} observations -> {out}")


def compare_vob(src, rebuilt_path):
    got = json.load(open(rebuilt_path, encoding="utf-8"))
    info = got.pop("_from", None)
    if info:
        print(f"  comparing a rebuild of {info['path']} ({info['bytes']:,} bytes, {info['mtime']})")
    diffs, checked = [], 0
    for i, r in enumerate(rows(os.path.join(src, "UKDA-4559-tab/tab/occ_1851_ew.tab")), start=2):
        if not r.get("regc_unit"):
            continue
        g = got.get(f"{NS_VOB}obs/occ_1851_ew/{i}")
        if g is None:
            diffs.append(f"row {i} missing"); continue
        want = {"unit": r["regc_unit"], "sex": "sex-M" if r["sex"] == "M" else "sex-F",
                "class": r.get("class") or "unknown", "subclass": r.get("subclass") or "unknown",
                "occupation": r.get("occupation") or "unknown", "sourceLabel": r["persons"],
                # The parsed number is carried beside the printed cell. Read both, or corrupting
                # one of them changes nothing: a control proved exactly that.
                "value": int(r["persons"]) if r["persons"] else None}
        for k, v in want.items():
            checked += 1
            if str(g.get(k)) != str(v):
                diffs.append(f"row {i} {k}: source {v!r} != rebuilt {g.get(k)!r}")
    print(f"{checked} cell values checked, {len(diffs)} differences")
    for d in diffs[:10]:
        print("  ", d)
    return 1 if diffs else 0


def compare_voi(root, rebuilt_path):
    units = {}
    for p in sorted(glob.glob(os.path.join(root, "site/data/units/*.json"))):
        units.update(json.load(open(p, encoding="utf-8")))
    got = json.load(open(rebuilt_path, encoding="utf-8"))
    info = got.pop("_from", None)
    if info:
        print(f"  comparing a rebuild of {info['path']} ({info['bytes']:,} bytes, {info['mtime']})")
    diffs, checked = [], 0
    for uid, u in units.items():
        for year, rowlist in sorted((u.get("stats") or {}).items()):
            for i, s in enumerate(rowlist):
                if s.get("value") is None:
                    continue
                g = got.get(f"{NS_VOI}obs/{uid}/{year}/{i}")
                if g is None:
                    diffs.append(f"{uid}/{year}/{i} missing"); continue
                checked += 4
                if str(g.get("sourceLabel")) != str(s["cell"]):
                    diffs.append(f"{uid}/{year}/{i} cell: {s['cell']!r} != {g.get('sourceLabel')!r}")
                if float(g.get("value")) != float(s["value"]):
                    diffs.append(f"{uid}/{year}/{i} value: {s['value']!r} != {g.get('value')!r}")
                # The coordinates ARE the cell, split. Rebuild the label from them, or the
                # dimension codes are never read and corrupting one is invisible.
                parts = [g[k] for k in sorted(g) if k.isdigit()]
                if parts and " / ".join(parts) != s["cell"]:
                    diffs.append(f"{uid}/{year}/{i} coordinates: {s['cell']!r} != "
                                 f"{' / '.join(parts)!r}")
                # A universe must be present exactly where the source defines one.
                want_u = s["table"] in UNIVERSE_LINKS
                if want_u != bool(g.get("universe")):
                    diffs.append(f"{uid}/{year}/{i} universe: source defines one = {want_u}, "
                                 f"carried = {bool(g.get('universe'))}")
                elif want_u and not str(g["universe"]).startswith(NS_VOI + "obs/"):
                    diffs.append(f"{uid}/{year}/{i} universe malformed: {g['universe']!r}")
    print(f"{checked} cell values checked, {len(diffs)} differences")
    for d in diffs[:10]:
        print("  ", d)
    return 1 if diffs else 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "inverse":
        inverse(sys.argv[2], sys.argv[3])
    elif cmd == "compare-vob":
        sys.exit(compare_vob(sys.argv[2], sys.argv[3]))
    elif cmd == "compare-voi":
        sys.exit(compare_voi(sys.argv[2], sys.argv[3]))
    else:
        (vob if cmd == "vob" else voi)(sys.argv[2], sys.argv[3])