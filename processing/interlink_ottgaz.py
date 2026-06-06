#!/usr/bin/env python
"""Interlink the ofs and og (ottgaz) authorities, and upgrade og geometry from Wikidata.

Two subcommands, both run ON pitt (prod ES on localhost:9201; staged extracts on
the shared /vast). Read-only/dry-run by default; pass --execute to write.

  relations  — resolve each ofs place's free-text kaza_1848/liva_1848 to the
               matching ottgaz admin unit (kaza-within-sancak, with maa/ve
               base-name folding) and append to the ofs place, in prod:
                 ofs place --within--> og:<kaza_id>   (resolved admin parent)
                 ofs place --within--> og:<sancak_id> (resolved sancak)
                 ofs place --within--> wd:Q…          ONLY when the matched
                       ottgaz unit carries a Wikidata QID — the QID is the
                       ADMIN UNIT's, so the place is *within* it (not closeMatch).
               This is the ofs→ottgaz→Wikidata bridge.

  wd-geometry — for og units linked to Wikidata, pull the geometry from our own
               wd:Q place record and UPGRADE the og geometry only when richer
               than the computed ofs hull (wd polygon > ofs hull > wd point);
               sets geometries[].source='wd'.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch import helpers as es_helpers

from processing.settings import STAGED_BASE_DIR

DEFAULT_ES_PASSWORD_FILE = "/ix1/ishi/es/config/elastic.password"

_TR = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ç": "c", "Ç": "c",
                     "ğ": "g", "Ğ": "g", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
                     "â": "a", "î": "i", "û": "u", "Î": "i", "Â": "a"})


def _norm(s):
    if not s:
        return ""
    s = s.translate(_TR)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _basename(s):
    return re.split(r"\s+ma['’ ]?a\s+|\s+ve\s+", s or "", maxsplit=1)[0]


def _es(es_host, pw_file):
    kw = {"request_timeout": 300}
    p = Path(pw_file)
    if p.exists():
        try:
            kw["basic_auth"] = ("elastic", p.read_text().strip())
        except PermissionError:
            pass
    return Elasticsearch(es_host, **kw)


def _og_extract():
    return Path(STAGED_BASE_DIR) / "og" / "extract" / "places.jsonl"


def _ofs_extract():
    return Path(STAGED_BASE_DIR) / "ofs" / "extract" / "places.jsonl"


# ---------------------------------------------------------------------------
# ottgaz resolution index (built from the og staged extract)
# ---------------------------------------------------------------------------

def load_og_index():
    docs = [json.loads(l) for l in _og_extract().open(encoding="utf-8") if l.strip()]
    id2name, id2unit, og_wd = {}, {}, {}
    for d in docs:
        oid = d["place_id"].split(":", 1)[1]
        id2name[oid] = d.get("title", "")
        id2unit[oid] = (d.get("admin_unit") or "").lower()
        for r in d.get("relations", []):
            rid = r.get("related_place_id", "")
            if rid.startswith("wd:"):
                og_wd[oid] = rid

    sancak_idx = {}                 # norm(name) -> og_id
    kaza_by_sancak = {}             # (norm sancak, norm kaza) -> og_id
    kaza_byname = defaultdict(list) # norm(kaza) -> [og_id]
    for d in docs:
        oid = d["place_id"].split(":", 1)[1]
        unit = id2unit[oid]
        nm = _norm(d.get("title"))
        if not nm:
            continue
        if unit in ("sancak", "liva"):
            sancak_idx.setdefault(nm, oid)
        elif unit == "kaza":
            kaza_byname[nm].append(oid)
            # sancak parent = a within-parent whose unit is sancak/liva
            for r in d.get("relations", []):
                rid = r.get("related_place_id", "")
                if rid.startswith("og:") and r.get("relation_type") == "within":
                    pid = rid.split(":", 1)[1]
                    if id2unit.get(pid) in ("sancak", "liva"):
                        kaza_by_sancak.setdefault((_norm(id2name.get(pid)), nm), oid)
    print(f"[og] units={len(id2name):,}  sancaks={len(sancak_idx):,}  "
          f"kaza(by sancak)={len(kaza_by_sancak):,}  with wd={len(og_wd):,}")
    return dict(sancak_idx=sancak_idx, kaza_by_sancak=kaza_by_sancak,
                kaza_byname=kaza_byname, og_wd=og_wd)


def _resolve(liva, kaza, idx):
    """Return (sancak_og_id|None, kaza_og_id|None) for an ofs place's admin names."""
    ln, kn = _norm(liva), _norm(kaza)
    kn_b = _norm(_basename(kaza))
    sancak_id = idx["sancak_idx"].get(ln)
    kaza_id = None
    for k in (kn, kn_b):
        if not k:
            continue
        kaza_id = idx["kaza_by_sancak"].get((ln, k))          # disambiguated
        if kaza_id:
            break
        cand = idx["kaza_byname"].get(k)
        if cand and len(cand) == 1:                            # unambiguous by name
            kaza_id = cand[0]
            break
    return sancak_id, kaza_id


# ---------------------------------------------------------------------------
# relations
# ---------------------------------------------------------------------------

def cmd_relations(args):
    idx = load_og_index()
    es = _es(args.es_host, args.es_password_file)

    updates = {}  # ofs place_id -> list[relation]
    n = matched_kaza = matched_sancak = with_wd = 0
    for line in _ofs_extract().open(encoding="utf-8"):
        if not line.strip():
            continue
        d = json.loads(line)
        n += 1
        liva = d.get("liva_1848"); kaza = d.get("kaza_1848")
        sancak_id, kaza_id = _resolve(liva, kaza, idx)
        rels = []
        if sancak_id:
            matched_sancak += 1
            rels.append({"relation_type": "within", "related_place_id": f"og:{sancak_id}",
                         "label": f"sancak: {liva} (ottgaz)"})
        if kaza_id:
            matched_kaza += 1
            rels.append({"relation_type": "within", "related_place_id": f"og:{kaza_id}",
                         "label": f"kaza: {kaza} (ottgaz)"})
            wd = idx["og_wd"].get(kaza_id) or (idx["og_wd"].get(sancak_id) if sancak_id else None)
            if wd:
                with_wd += 1
                rels.append({"relation_type": "within", "related_place_id": wd,
                             "label": "admin unit (Wikidata, via ottgaz)"})
        if rels:
            updates[d["place_id"]] = rels

    print(f"[relations] ofs places: {n:,}")
    print(f"  resolved sancak→og: {matched_sancak:,}  kaza→og: {matched_kaza:,}  "
          f"+wikidata bridge: {with_wd:,}")
    print(f"  ofs places gaining ≥1 relation: {len(updates):,}")
    if not args.execute:
        sample = list(updates.items())[:3]
        for pid, rels in sample:
            print(f"    {pid}: {[r['related_place_id'] for r in rels]}")
        print("[relations] DRY-RUN — no writes.")
        return

    idxname = _concrete(es, "places")
    actions = ({"_op_type": "update", "_index": idxname, "_id": pid,
                "script": {"source": _APPEND_RELS, "lang": "painless",
                           "params": {"rels": rels}}}
               for pid, rels in updates.items())
    ok, errs = es_helpers.bulk(es, actions, chunk_size=1000, raise_on_error=False)
    es.indices.refresh(index=idxname)
    print(f"[relations] updated ok={ok:,} errors={len(errs) if isinstance(errs, list) else errs}")


_APPEND_RELS = """
if (ctx._source.relations == null) { ctx._source.relations = []; }
for (r in params.rels) {
  boolean exists = false;
  for (er in ctx._source.relations) {
    if (er.related_place_id == r.related_place_id) { exists = true; break; }
  }
  if (!exists) { ctx._source.relations.add(r); }
}
"""


def _concrete(es, alias):
    return list(es.indices.get_alias(name=alias).keys())[0]


# ---------------------------------------------------------------------------
# wd-geometry upgrade
# ---------------------------------------------------------------------------

def cmd_wd_geometry(args):
    es = _es(args.es_host, args.es_password_file)
    docs = [json.loads(l) for l in _og_extract().open(encoding="utf-8") if l.strip()]
    linked = [(d["place_id"], d["wikidata_qid"], bool(d.get("geometries")))
              for d in docs if d.get("wikidata_qid")]
    print(f"[wd-geometry] og units with a Wikidata link: {len(linked):,}")

    qids = [q for _, q, _ in linked]
    wd_geom = {}  # qid -> geom_entry dict (repr_point / has_geom / geom_ref / hull)
    for i in range(0, len(qids), 500):
        chunk = qids[i:i + 500]
        res = es.search(index="places", size=len(chunk),
                        query={"terms": {"place_id": [f"wd:{q}" for q in chunk]}},
                        _source=["place_id", "geometries"])
        for h in res["hits"]["hits"]:
            pid_wd = h["_source"]["place_id"]
            q = pid_wd.split(":", 1)[1]
            geoms = h["_source"].get("geometries") or []
            if geoms:
                g = geoms[0]
                has = bool(g.get("has_geom"))
                wd_geom[q] = {
                    "repr_point": g.get("repr_point"),
                    "has_geom": has,
                    # reference the wd polygon in the shared geom store by its key
                    "geom_ref": g.get("geom_ref") or (
                        f'{pid_wd}_{g.get("geometry_index", 0)}' if has else None),
                    "hull": g.get("hull"),
                }

    def _build(wg):
        """wd polygon > ofs hull > wd point — return the og geom_entry or None."""
        if not wg or not wg.get("repr_point"):
            return None
        if wg["has_geom"]:  # wd has a polygon → reference it (overrides ofs hull)
            return {"has_geom": True, "geom_ref": wg["geom_ref"], "repr_point": wg["repr_point"],
                    "hull": wg.get("hull"), "source": "wd", "approximation": "exact",
                    "timespans": []}
        return {"has_geom": False, "repr_point": wg["repr_point"], "source": "wd",
                "approximation": "centroid", "timespans": []}  # wd point only

    upgrades = []  # (pid, geom_entry)
    polys = 0
    for pid, qid, has_ofs_hull in linked:
        wg = wd_geom.get(qid)
        geom = _build(wg)
        if not geom:
            continue
        # apply when wd is a polygon (richer than the hull) OR og has no hull
        if geom["has_geom"]:
            polys += 1
            upgrades.append((pid, geom))
        elif not has_ofs_hull:
            upgrades.append((pid, geom))

    print(f"[wd-geometry] wd records with geometry: {len(wd_geom):,}  "
          f"og upgrades: {len(upgrades):,}  (of which wd POLYGONS: {polys:,})")
    if not args.execute:
        print("  sample:", [(p, g["source"], g["approximation"], g["has_geom"]) for p, g in upgrades[:3]])
        print("[wd-geometry] DRY-RUN — no writes.")
        return

    idxname = _concrete(es, "places")
    now = datetime.now(timezone.utc).isoformat()

    # (1) prod
    def actions():
        for pid, geom in upgrades:
            yield {"_op_type": "update", "_index": idxname, "_id": pid,
                   "doc": {"geometries": [geom], "indexed_at": now}}
    ok, errs = es_helpers.bulk(es, actions(), chunk_size=500, raise_on_error=False)
    es.indices.refresh(index=idxname)
    print(f"[wd-geometry] prod updated ok={ok:,} errors={len(errs) if isinstance(errs, list) else errs}")

    # (2) staged extract — the tiles' source of truth; keep it consistent with
    # prod so generate_tiles (which reads staged, not ES) renders these units.
    up = {pid: geom for pid, geom in upgrades}
    ext = _og_extract()
    out_lines, patched = [], 0
    for line in ext.open(encoding="utf-8"):
        if not line.strip():
            continue
        d = json.loads(line)
        if d["place_id"] in up:
            d["geometries"] = [up[d["place_id"]]]
            patched += 1
        out_lines.append(json.dumps(d, ensure_ascii=False))
    tmp = ext.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(ext)
    print(f"[wd-geometry] staged extract patched: {patched:,} docs → {ext}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    for name, fn in (("relations", cmd_relations), ("wd-geometry", cmd_wd_geometry)):
        sp = sub.add_parser(name)
        sp.add_argument("--es-host", required=True)
        sp.add_argument("--es-password-file", default=DEFAULT_ES_PASSWORD_FILE)
        sp.add_argument("--execute", action="store_true", help="apply writes (default: dry-run)")
        sp.set_defaults(func=fn)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
